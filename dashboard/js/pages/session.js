// ═══════════════════════════════════════════════════════════════
// session.js — Session & cookies page module
//
// Consolidates warmup robot + cookie pool + Chrome import into
// one page. Previously (Phase 2) these lived on the profile detail
// page; split out so profiles/ stays compact.
//
// API:
//   GET  /api/warmup/presets
//   GET  /api/session/<profile>
//   POST /api/warmup/<profile>/run
//   GET  /api/warmup/<profile>/history
//   GET  /api/snapshots/<profile>
//   DELETE /api/snapshots/entry/<id>
//   POST /api/snapshots/<profile>/<id>/restore
//   GET  /api/profiles/<profile>/cookies                 (legacy)
//   POST /api/profiles/<profile>/cookies/clear           (legacy)
//   POST /api/profiles/<profile>/chrome-import           (legacy)
// ═══════════════════════════════════════════════════════════════

const SessionPage = (() => {

  const state = {
    profiles:        [],
    currentProfile:  null,
    presets:         [],
    selectedPreset:  "general",
    status:          null,
    warmupHistory:   [],
    snapshots:       [],
    cookies:         [],
    cookieFilter:    "",
    currentTab:      "warmup",
    pollTimer:       null,
  };

  // ─────────────────────────────────────────────────────────────
  // init
  // ─────────────────────────────────────────────────────────────
  async function init() {
    bindEvents();

    try {
      const [profilesResp, presetsResp] = await Promise.all([
        api("/api/profiles"),
        api("/api/warmup/presets"),
      ]);
      state.profiles = (profilesResp || []).map(p => p.name || p);
      state.presets  = presetsResp.presets || [];
    } catch (e) {
      toast("Failed to load: " + e.message, true);
      return;
    }

    renderProfileNav();
    renderPresets();

    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const pre = params.get("profile") || state.profiles[0] || null;
    if (pre) {
      await selectProfile(pre);   // nav-list active state comes from renderProfileNav
    }
  }

  function teardown() {
    if (state.pollTimer) clearInterval(state.pollTimer);
  }

  // ─────────────────────────────────────────────────────────────
  // Wiring
  // ─────────────────────────────────────────────────────────────
  function bindEvents() {
    // Profile nav — delegated click handler. Picking a row triggers
    // selectProfile(); active-state styling is applied in renderProfileNav.
    $("#session-profile-list").addEventListener("click", (e) => {
      const row = e.target.closest(".session-profile-row");
      if (!row) return;
      selectProfile(row.dataset.profile);
      location.hash = `#session?profile=${encodeURIComponent(row.dataset.profile)}`;
    });

    // Warmup
    $("#session-warmup-run-btn").addEventListener("click", runWarmup);

    // Cookies tab
    $("#sess-cookies-reload").addEventListener("click", () => loadCookies());
    $("#sess-cookies-import").addEventListener("click", openCookieImportModal);
    $("#sess-cookies-export").addEventListener("click", exportCookies);
    $("#sess-cookies-clear").addEventListener("click",  clearCookies);
    $("#sess-cookies-filter").addEventListener("input", (e) => {
      state.cookieFilter = (e.target.value || "").toLowerCase();
      renderCookies();
    });
    $("#sess-cookie-import-submit").addEventListener("click", submitCookieImport);

    // Snapshots — delegated below in render

    // Chrome import
    $("#sess-import-run-btn").addEventListener("click", runChromeImport);

    // Tabs
    $("#session-tabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".fp-tab");
      if (tab) switchTab(tab.dataset.tab);
    });

    // Modal close (any [data-close])
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-close]");
      if (!t) return;
      const m = document.getElementById(t.dataset.close);
      if (m) m.style.display = "none";
    });
  }

  function switchTab(name) {
    state.currentTab = name;
    $$(".fp-tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    $$(".fp-tabpane").forEach(p => p.classList.toggle("active", p.dataset.tabpane === name));
    // Lazy-load cookies on first entry (avoid loading 50 cookies when the
    // user is only looking at warmup).
    if (name === "cookies" && !state.cookies.length) loadCookies();
  }

  // ─────────────────────────────────────────────────────────────
  // Profile selector
  // ─────────────────────────────────────────────────────────────
  function renderProfileNav() {
    const host     = $("#session-profile-list");
    const countEl  = $("#session-profile-count");
    const emptyEl  = $("#session-empty-state");
    const layoutEl = $("#session-layout");

    // No profiles at all → hide the whole workspace, show empty-state CTA
    if (!state.profiles.length) {
      if (emptyEl)  emptyEl.style.display  = "flex";
      if (layoutEl) layoutEl.style.display = "none";
      if (countEl)  countEl.textContent    = "0";
      if (host)     host.innerHTML = "";
      return;
    }
    if (emptyEl)  emptyEl.style.display  = "none";
    if (layoutEl) layoutEl.style.display = "grid";
    if (countEl)  countEl.textContent    = String(state.profiles.length);

    // Render cards — active state follows state.currentProfile so the
    // left-border accent moves as the user clicks around.
    host.innerHTML = state.profiles.map(name => {
      const active = state.currentProfile === name;
      // Tiny per-card hint — filled in later if we have cached status.
      // We deliberately don't fetch per-profile status upfront because
      // that's N requests on a page load; the active row reveals its
      // data via the statbar in .session-content.
      return `
        <div class="session-profile-row ${active ? "active" : ""}"
             data-profile="${escapeHtml(name)}"
             role="button" tabindex="0">
          <div class="session-profile-row-dot"></div>
          <div class="session-profile-row-body">
            <div class="session-profile-row-name">${escapeHtml(name)}</div>
            <div class="session-profile-row-meta" data-profile-meta="${escapeHtml(name)}"></div>
          </div>
        </div>
      `;
    }).join("");
  }

  async function selectProfile(name) {
    if (!name) return;
    state.currentProfile = name;
    state.cookies = [];
    renderProfileNav();   // move the active border immediately — feel snappy
    await refreshStatus();
    await loadWarmupHistory();
    await loadSnapshots();
    startPolling();
    renderAll();
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    // Light poll — status rollup only (cheap). Enough to catch
    // warmup progress updates from the background thread.
    state.pollTimer = setInterval(async () => {
      if (state.currentProfile && state.currentTab === "warmup") {
        try { await refreshStatus(); renderSummaryStrip(); renderWarmupLive(); }
        catch {}
      }
    }, 3000);
  }

  async function refreshStatus() {
    if (!state.currentProfile) return;
    try {
      state.status = await api(`/api/session/${encodeURIComponent(state.currentProfile)}`);
    } catch (e) { state.status = null; }
  }

  async function loadWarmupHistory() {
    try {
      const r = await api(`/api/warmup/${encodeURIComponent(state.currentProfile)}/history?limit=30`);
      state.warmupHistory = r.history || [];
    } catch { state.warmupHistory = []; }
  }

  async function loadSnapshots() {
    try {
      const r = await api(`/api/snapshots/${encodeURIComponent(state.currentProfile)}`);
      state.snapshots = r.snapshots || [];
    } catch { state.snapshots = []; }
  }

  async function loadCookies() {
    if (!state.currentProfile) return;
    try {
      const r = await api(`/api/profiles/${encodeURIComponent(state.currentProfile)}/cookies`);
      state.cookies = r.cookies || r || [];
    } catch (e) {
      state.cookies = [];
      toast("Cookies load failed: " + e.message, true);
    }
    renderCookies();
  }

  // ─────────────────────────────────────────────────────────────
  // Render everything that depends on state
  // ─────────────────────────────────────────────────────────────
  function renderAll() {
    renderProfileNav();   // in case profile list changed (rare)
    renderSummaryStrip();
    renderPresets();
    renderWarmupLive();
    renderWarmupHistory();
    renderSnapshots();
    renderTabBadges();
  }

  function renderSummaryStrip() {
    const last  = state.status?.warmup?.last;
    const stats = state.status?.snapshots || {};
    const running = state.status?.warmup?.running;

    $("#stat-last-warmup").textContent = last ? timeAgo(last.started_at) : "—";
    $("#stat-last-warmup-sub").textContent = last
      ? `${last.preset} · ${last.sites_succeeded}/${last.sites_planned} ok`
      : "never run";

    $("#stat-warmup-status").textContent = running ? "running" : (last?.status || "—");
    $("#stat-warmup-status").className = "dense-stat-value " +
      (running ? "stat-ok" :
       last?.status === "ok" ? "stat-ok" :
       last?.status === "failed" ? "stat-err" : "");

    $("#stat-snapshots").textContent = stats.n ?? "—";
    $("#stat-snapshots-sub").textContent = stats.last_at ? `last ${timeAgo(stats.last_at)}` : "—";
    $("#stat-pool-cookies").textContent = stats.total_cookies ?? "—";
    $("#stat-pool-bytes").textContent = stats.total_bytes ? formatBytes(stats.total_bytes) : "—";
  }

  function renderPresets() {
    const host = $("#session-preset-grid");
    if (!host) return;
    if (!state.presets.length) {
      host.innerHTML = '<div class="dense-empty" style="grid-column:1/-1;">No presets.</div>';
      return;
    }
    host.innerHTML = state.presets.map(p => `
      <label class="session-preset-card ${state.selectedPreset === p.id ? 'selected' : ''}">
        <input type="radio" name="session-preset" value="${escapeHtml(p.id)}"
               ${state.selectedPreset === p.id ? 'checked' : ''}>
        <div class="session-preset-body">
          <div class="session-preset-label">${escapeHtml(p.label)}
            <span class="session-preset-count">${p.site_count} sites</span>
          </div>
          <div class="session-preset-desc">${escapeHtml(p.description)}</div>
        </div>
      </label>
    `).join("");
    host.querySelectorAll('input[name="session-preset"]').forEach(r => {
      r.addEventListener("change", (e) => {
        state.selectedPreset = e.target.value;
        renderPresets();
      });
    });
  }

  function renderWarmupLive() {
    const running = !!state.status?.warmup?.running;
    const box = $("#session-warmup-live");
    const btn = $("#session-warmup-run-btn");
    box.style.display = running ? "flex" : "none";
    btn.disabled = running;
    btn.textContent = running ? "⏳ Running…" : "▶ Run warmup now";
    if (running) {
      const last = state.status?.warmup?.last;
      $("#session-warmup-live-title").textContent = "Warmup in progress";
      $("#session-warmup-live-sub").textContent   = last
        ? `${last.sites_visited}/${last.sites_planned} sites visited`
        : "starting up…";
    }
  }

  function renderWarmupHistory() {
    const tbody = $("#session-warmup-tbody");
    if (!state.warmupHistory.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="dense-empty-cell">No warmups yet.</td></tr>';
      return;
    }
    tbody.innerHTML = state.warmupHistory.map(h => {
      const dur = h.duration_sec ? `${h.duration_sec.toFixed(1)}s` : "—";
      const statusCls =
        h.status === "ok"      ? "stat-ok" :
        h.status === "partial" ? ""        :
        h.status === "running" ? "stat-ok" :
        "stat-err";
      const sites = h.sites_planned
        ? `${h.sites_succeeded || 0}/${h.sites_planned}`
        : "—";
      return `
        <tr>
          <td>${fmtTimestamp(h.started_at)}</td>
          <td>${escapeHtml(h.preset || "—")}</td>
          <td class="muted">${escapeHtml(h.trigger || "—")}</td>
          <td class="num">${sites}</td>
          <td class="num">${dur}</td>
          <td class="${statusCls}">${escapeHtml(h.status)}</td>
          <td class="muted">${escapeHtml(h.notes || "")}</td>
        </tr>
      `;
    }).join("");
  }

  function renderSnapshots() {
    const tbody = $("#sess-snapshots-tbody");
    if (!state.snapshots.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="dense-empty-cell">No snapshots yet. Run a clean monitoring session to seed the pool.</td></tr>';
      return;
    }
    tbody.innerHTML = state.snapshots.map(s => `
      <tr>
        <td>${fmtTimestamp(s.created_at)}</td>
        <td class="muted">${escapeHtml(s.trigger || "—")}</td>
        <td class="num">${s.cookie_count}</td>
        <td class="num">${s.domain_count}</td>
        <td class="num">${formatBytes(s.bytes || 0)}</td>
        <td class="muted">${escapeHtml(s.reason || "—")}</td>
        <td>
          <button class="btn btn-secondary btn-small" data-restore="${s.id}"
                  title="Queue this snapshot for injection on the next launch">
            ⬅ Restore
          </button>
          <button class="btn btn-secondary btn-small btn-danger" data-delete="${s.id}">
            ✕
          </button>
        </td>
      </tr>
    `).join("");

    tbody.querySelectorAll("[data-restore]").forEach(b =>
      b.addEventListener("click", () => restoreSnapshot(+b.dataset.restore)));
    tbody.querySelectorAll("[data-delete]").forEach(b =>
      b.addEventListener("click", () => deleteSnapshot(+b.dataset.delete)));
  }

  function renderCookies() {
    const tbody = $("#sess-cookies-tbody");
    const hint  = $("#sess-cookies-hint");
    if (!state.cookies.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="dense-empty-cell">No cookies stored for this profile.</td></tr>';
      hint.textContent = "";
      return;
    }
    const filter = state.cookieFilter;
    const filtered = filter
      ? state.cookies.filter(c =>
          (c.name || "").toLowerCase().includes(filter) ||
          (c.domain || "").toLowerCase().includes(filter))
      : state.cookies;
    hint.textContent = `— ${filtered.length} of ${state.cookies.length} shown`;
    tbody.innerHTML = filtered.map(c => {
      const flags = [];
      if (c.secure)    flags.push('<span class="session-flag flag-secure" title="Secure">S</span>');
      if (c.httpOnly)  flags.push('<span class="session-flag flag-http" title="HttpOnly">H</span>');
      if (c.is_host_only || c.hostOnly) flags.push('<span class="session-flag flag-host" title="Host-only">N</span>');
      if (c.sameSite && c.sameSite !== "none" && c.sameSite !== "unspecified")
        flags.push(`<span class="session-flag flag-ss" title="SameSite=${escapeHtml(c.sameSite)}">L</span>`);

      const expires = c.expires
        ? (typeof c.expires === "number"
            ? new Date(c.expires * 1000).toISOString().slice(0, 10)
            : String(c.expires).slice(0, 10))
        : "session";
      const value = c.value == null
        ? "(encrypted — run profile to decrypt)"
        : escapeHtml(String(c.value).slice(0, 60));
      return `
        <tr>
          <td><strong>${escapeHtml(c.name || "")}</strong></td>
          <td><code class="muted">${escapeHtml(c.domain || "")}</code></td>
          <td class="muted" style="font-family: ui-monospace, monospace;">${value}</td>
          <td>${escapeHtml(expires)}</td>
          <td>${flags.join(" ") || '<span class="muted">—</span>'}</td>
          <td><button class="btn btn-secondary btn-small btn-danger"
                      data-cookie-del="${escapeHtml(c.name || "")}"
                      title="Delete this cookie">✕</button></td>
        </tr>`;
    }).join("");
    tbody.querySelectorAll("[data-cookie-del]").forEach(b =>
      b.addEventListener("click", () => deleteCookie(b.dataset.cookieDel)));
  }

  function renderTabBadges() {
    $("#sess-tab-cookies-badge").textContent = state.cookies.length || "—";
    $("#sess-tab-snapshots-badge").textContent = state.snapshots.length || "—";
  }

  // ─────────────────────────────────────────────────────────────
  // Actions
  // ─────────────────────────────────────────────────────────────
  async function runWarmup() {
    if (!state.currentProfile) return;
    const sites = parseInt($("#session-warmup-sites").value, 10) || 7;
    try {
      await api(`/api/warmup/${encodeURIComponent(state.currentProfile)}/run`, {
        method: "POST",
        body: JSON.stringify({ preset: state.selectedPreset, sites,
                                trigger: "manual" }),
      });
      toast("Warmup started");
      await refreshStatus();
      renderWarmupLive();
      renderSummaryStrip();
    } catch (e) {
      toast("Warmup failed to start: " + e.message, true);
    }
  }

  async function restoreSnapshot(sid) {
    if (!await confirmDialog({
      title: "Restore snapshot?",
      message: "This will inject the snapshot's cookies + localStorage into the profile the NEXT TIME it launches. Any active run should be stopped first.",
      confirmText: "Restore",
    })) return;
    try {
      await api(`/api/snapshots/${encodeURIComponent(state.currentProfile)}/${sid}/restore`,
                { method: "POST" });
      toast("Restore queued for next launch");
    } catch (e) { toast("Restore failed: " + e.message, true); }
  }

  async function deleteSnapshot(sid) {
    if (!await confirmDialog({
      title: "Delete snapshot?",
      message: "This snapshot will be removed permanently.",
      confirmText: "Delete", confirmStyle: "danger",
    })) return;
    try {
      await api(`/api/snapshots/entry/${sid}`, { method: "DELETE" });
      toast("Snapshot deleted");
      await loadSnapshots();
      renderSnapshots();
      renderTabBadges();
    } catch (e) { toast("Delete failed: " + e.message, true); }
  }

  async function clearCookies() {
    if (!await confirmDialog({
      title: "Clear all cookies?",
      message: "Removes every stored cookie for this profile. Next run starts fresh.",
      confirmText: "Clear", confirmStyle: "danger",
    })) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(state.currentProfile)}/cookies/clear`,
                { method: "POST" });
      await loadCookies();
      toast("Cookies cleared");
    } catch (e) { toast("Clear failed: " + e.message, true); }
  }

  async function deleteCookie(name) {
    try {
      await api(`/api/profiles/${encodeURIComponent(state.currentProfile)}/cookies/${encodeURIComponent(name)}`,
                { method: "DELETE" });
      await loadCookies();
    } catch (e) { toast("Delete failed: " + e.message, true); }
  }

  async function exportCookies() {
    const url = `/api/profiles/${encodeURIComponent(state.currentProfile)}/cookies/export?format=json`;
    const a = document.createElement("a");
    a.href = url;
    a.download = `cookies-${state.currentProfile}.json`;
    a.click();
  }

  function openCookieImportModal() {
    $("#sess-cookie-import-text").value = "";
    $("#sess-cookie-import-modal").style.display = "flex";
  }

  async function submitCookieImport() {
    const txt = $("#sess-cookie-import-text").value;
    if (!txt.trim()) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(state.currentProfile)}/cookies/import`,
                { method: "POST", body: JSON.stringify({ cookies: txt }) });
      toast("Cookies imported");
      $("#sess-cookie-import-modal").style.display = "none";
      await loadCookies();
    } catch (e) { toast("Import failed: " + e.message, true); }
  }

  async function runChromeImport() {
    if (!state.currentProfile) return;
    const body = {
      source:            $("#sess-import-source").value.trim() || null,
      days:              parseInt($("#sess-import-days").value, 10) || 90,
      max_urls:          parseInt($("#sess-import-maxurls").value, 10) || 5000,
      skip_sensitive:    $("#sess-import-sensitive").checked,
    };
    const btn = $("#sess-import-run-btn");
    const status = $("#sess-import-status");
    btn.disabled = true; status.textContent = "Importing…";
    try {
      const r = await api(`/api/profiles/${encodeURIComponent(state.currentProfile)}/chrome-import`,
                          { method: "POST", body: JSON.stringify(body) });
      status.textContent = `✓ ${r.urls_imported ?? 0} URLs, ${r.bookmarks_imported ?? 0} bookmarks`;
      toast("Chrome data imported");
    } catch (e) {
      status.textContent = "";
      toast("Import failed: " + e.message, true);
    } finally { btn.disabled = false; }
  }

  return { init, teardown };
})();
