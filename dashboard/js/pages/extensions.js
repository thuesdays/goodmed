// ═══════════════════════════════════════════════════════════════
// pages/extensions.js — Extensions pool manager.
//
// Pool model:
//   one shared library on disk (data/extensions_pool/<id>/)
//   each profile picks a subset → --load-extension at launch
//   per-profile data lives in user-data-dir (Local Extension Settings,
//     IndexedDB, etc) so wallets/accounts stay isolated
//
// UI:
//   left  — grid of ExtensionCard nodes (icon, name, version, "used by N")
//   right — detail panel (manifest summary, assigned-profiles toggle,
//           enable/disable, reinstall, remove)
//
// Backend endpoints — all in server.py (Phase 2):
//   GET    /api/extensions
//   GET    /api/extensions/<id>
//   PATCH  /api/extensions/<id>           { name?, is_enabled?, auto_install_for_new? }
//   DELETE /api/extensions/<id>
//   POST   /api/extensions/upload         (multipart: file=...)
//   POST   /api/extensions/install-cws    { id_or_url }
//   GET    /api/extensions/cws-search?q=…
//   GET    /api/profiles
//   GET    /api/profiles/<name>/extensions
//   POST   /api/profiles/<name>/extensions       { extension_id, enabled? }
//   DELETE /api/profiles/<name>/extensions/<ext_id>
// ═══════════════════════════════════════════════════════════════

const ExtensionsPage = (() => {

  // Curated "verified" wallet/utility IDs so users don't grab a clone
  // off the CWS by accident. Source: each extension's official site.
  // Last verified 2026-04 — IDs change rarely, but if a vendor moves
  // their listing this list might go stale.
  const RECOMMENDED = [
    { id: "nkbihfbeogaeaoehlefnkodbefgpgknn", name: "MetaMask",       category: "wallet" },
    { id: "mcohilncbfahbmgdjkbpemcciiolgcge", name: "OKX Wallet",     category: "wallet" },
    { id: "bfnaelmomeimhlpmgjnjophhpkkoljpa", name: "Phantom",        category: "wallet" },
    { id: "egjidjbpglichdcondbcbdnbeeppgdph", name: "Trust Wallet",   category: "wallet" },
    { id: "fhbohimaelbohpjbbldcngcnapndodjp", name: "Binance Wallet", category: "wallet" },
    { id: "hnfanknocfeofbddgcijnmhnfnkdnaad", name: "Coinbase Wallet", category: "wallet" },
    { id: "cjpalhdlnbpafiamejdnhcphjbkeiagm", name: "uBlock Origin",  category: "utility" },
  ];

  const state = {
    pool:           [],     // [{ id, name, version, source, ... }]
    profiles:       [],     // [{ name, ... }]
    profileMap:     {},     // ext_id -> [profile names that use it]
    selectedId:     null,
    filterSource:   "",
    filterSearch:   "",
    searchTimer:    null,
    detail:         null,   // last fetched extension detail
  };

  async function init() {
    bindEvents();
    await reloadAll();
  }

  function teardown() {
    // No timers/sockets to clean up.
  }

  // ─────────────────────────────────────────────────────────────
  // Event wiring
  // ─────────────────────────────────────────────────────────────
  function bindEvents() {
    $("#ext-reload-btn")?.addEventListener("click", () => reloadAll());

    // "+ Add" dropdown
    const addBtn  = $("#ext-add-btn");
    const addMenu = $("#ext-add-menu");
    addBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = addMenu.style.display !== "none";
      addMenu.style.display = open ? "none" : "block";
    });
    document.addEventListener("click", (e) => {
      if (!addMenu) return;
      if (addMenu.style.display === "none") return;
      if (!addMenu.contains(e.target) && e.target !== addBtn) {
        addMenu.style.display = "none";
      }
    });
    addMenu?.querySelectorAll(".ext-add-menu-item").forEach(item => {
      item.addEventListener("click", () => {
        addMenu.style.display = "none";
        const src = item.dataset.source;
        if (src === "crx")        $("#ext-file-crx")?.click();
        if (src === "folder")     $("#ext-file-folder")?.click();
        if (src === "cws-id")     openCwsIdModal();
        if (src === "cws-search") openCwsSearchModal();
      });
    });

    // File upload pickers
    $("#ext-file-crx")?.addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (f) uploadFile(f, "crx");
      e.target.value = "";
    });
    $("#ext-file-folder")?.addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (f) uploadFile(f, "folder");
      e.target.value = "";
    });

    // Search filter
    $("#ext-search")?.addEventListener("input", (e) => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.filterSearch = (e.target.value || "").trim().toLowerCase();
        renderGrid();
      }, 150);
    });

    // Source filter (segmented). Each button has data-source for the
    // canonical DB value and optionally data-source-alt as a fallback
    // (CRX uploads are tagged "manual_crx" in the DB but we expose
    // them under the friendlier "crx" badge in the card pills).
    document.querySelectorAll(".ext-source-filter [data-source]").forEach(b => {
      b.addEventListener("click", () => {
        document.querySelectorAll(".ext-source-filter [data-source]")
          .forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        state.filterSource    = b.dataset.source || "";
        state.filterSourceAlt = b.dataset.sourceAlt || "";
        renderGrid();
      });
    });

    // CWS install-by-ID modal
    document.querySelectorAll('[data-close="ext-cws-id-modal"]').forEach(el => {
      el.addEventListener("click", () => closeModal("ext-cws-id-modal"));
    });
    $("#ext-cws-id-install-btn")?.addEventListener("click", () => installFromCws());
    $("#ext-cws-id-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); installFromCws(); }
    });

    // CWS search modal
    document.querySelectorAll('[data-close="ext-cws-search-modal"]').forEach(el => {
      el.addEventListener("click", () => closeModal("ext-cws-search-modal"));
    });
    $("#ext-cws-search-btn")?.addEventListener("click", () => runCwsSearch());
    $("#ext-cws-search-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); runCwsSearch(); }
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Data load
  // ─────────────────────────────────────────────────────────────
  async function reloadAll() {
    try {
      const [pool, profiles] = await Promise.all([
        api("/api/extensions").catch(() => ({ extensions: [] })),
        api("/api/profiles").catch(() => ({ profiles: [] })),
      ]);
      state.pool     = pool?.extensions || pool || [];
      state.profiles = profiles?.profiles || profiles || [];
      await rebuildProfileMap();
    } catch (e) {
      toast(`Failed to load: ${e.message}`, true);
      state.pool = [];
      state.profiles = [];
      state.profileMap = {};
    }
    renderStats();
    renderGrid();
    if (state.selectedId) {
      const still = state.pool.find(x => x.id === state.selectedId);
      if (still) renderDetail(state.selectedId);
      else       clearDetail();
    }
  }

  // For each extension, figure out which profiles assigned it. We do
  // this with one fan-out across profiles. Cheap because the assignment
  // table is small (a few rows per profile) and only happens on
  // page-load + after assign/remove ops.
  async function rebuildProfileMap() {
    const m = {};
    for (const p of state.profiles) {
      try {
        const r = await api(`/api/profiles/${encodeURIComponent(p.name)}/extensions`);
        const list = r?.extensions || r || [];
        for (const row of list) {
          const eid = row.extension_id || row.id;
          if (!eid) continue;
          (m[eid] = m[eid] || []).push({
            name:    p.name,
            enabled: row.enabled !== 0 && row.enabled !== false,
          });
        }
      } catch {}
    }
    state.profileMap = m;
  }

  // ─────────────────────────────────────────────────────────────
  // Stat strip
  // ─────────────────────────────────────────────────────────────
  function renderStats() {
    const total    = state.pool.length;
    const enabled  = state.pool.filter(x => x.is_enabled !== 0 && x.is_enabled !== false).length;
    const disabled = total - enabled;
    const profilesUsing = new Set();
    for (const eid of Object.keys(state.profileMap)) {
      for (const p of state.profileMap[eid]) profilesUsing.add(p.name);
    }
    $("#ext-stat-total").textContent    = total;
    $("#ext-stat-enabled").textContent  = enabled;
    $("#ext-stat-disabled").textContent = disabled;
    $("#ext-stat-profiles").textContent = profilesUsing.size;

    // Sidebar badge mirror
    const b = document.getElementById("badge-extensions");
    if (b) {
      b.textContent = String(total);
      b.style.display = total > 0 ? "" : "none";
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Grid
  // ─────────────────────────────────────────────────────────────
  function renderGrid() {
    const grid = $("#ext-grid");
    if (!grid) return;

    const q    = state.filterSearch;
    const src  = state.filterSource;
    const sAlt = state.filterSourceAlt || "";

    const matches = state.pool.filter(x => {
      const xSrc = (x.source || "").toLowerCase();
      if (src && xSrc !== src && (!sAlt || xSrc !== sAlt)) return false;
      if (q) {
        const hay = `${x.name || ""} ${x.id || ""} ${x.source || ""} ${x.description || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    // Update grid count badge in panel header
    const badge = document.getElementById("ext-grid-count");
    if (badge) badge.textContent = String(matches.length);

    if (!matches.length) {
      grid.innerHTML = `
        <div class="dense-empty" style="padding: 40px 16px; text-align: center;">
          <div style="font-size: 32px; opacity: 0.4; margin-bottom: 10px;">📭</div>
          <div style="font-size: 13px; opacity: 0.7;">
            ${state.pool.length === 0
              ? "No extensions in the pool yet. Click <strong>+ Add extension</strong> to install your first one."
              : "No extensions match the current filter."}
          </div>
        </div>`;
      return;
    }

    grid.innerHTML = matches.map(x => renderCard(x)).join("");
    grid.querySelectorAll("[data-ext-id]").forEach(card => {
      card.addEventListener("click", () => {
        state.selectedId = card.dataset.extId;
        grid.querySelectorAll("[data-ext-id]").forEach(c => c.classList.remove("selected"));
        card.classList.add("selected");
        renderDetail(state.selectedId);
      });
    });

    // Re-apply selection highlight if we still have one
    if (state.selectedId) {
      const sel = grid.querySelector(`[data-ext-id="${cssEscape(state.selectedId)}"]`);
      sel?.classList.add("selected");
    }
  }

  function renderCard(x) {
    const profiles = state.profileMap[x.id] || [];
    const usedBy   = profiles.length;
    const isOff    = x.is_enabled === 0 || x.is_enabled === false;
    const icon     = x.icon_b64
      ? `<img class="ext-card-icon-img" src="${(x.icon_b64.startsWith('data:') ? x.icon_b64 : 'data:image/png;base64,' + x.icon_b64)}" alt="">`
      : `<div class="ext-card-icon-fallback">🧩</div>`;

    const sourceBadge = sourceLabel(x.source);
    const v = x.version ? `v${escapeHtml(x.version)}` : "";

    return `
      <div class="ext-card ${isOff ? "is-disabled" : ""}" data-ext-id="${escapeHtml(x.id)}">
        <div class="ext-card-icon">${icon}</div>
        <div class="ext-card-body">
          <div class="ext-card-name" title="${escapeHtml(x.name)}">
            ${escapeHtml(x.name || "(unnamed)")}
            ${isOff ? '<span class="ext-card-off-pill">disabled</span>' : ""}
          </div>
          <div class="ext-card-meta">
            <span class="ext-source-pill ext-source-${escapeHtml(x.source || "unknown")}">${sourceBadge}</span>
            ${v ? `<span class="ext-card-version">${v}</span>` : ""}
          </div>
          <div class="ext-card-usage">
            ${usedBy > 0
              ? `<span class="ext-card-usage-num">${usedBy}</span> profile${usedBy === 1 ? "" : "s"}`
              : `<span style="opacity: 0.55;">no profiles assigned</span>`}
          </div>
        </div>
      </div>`;
  }

  function sourceLabel(s) {
    if (s === "cws")    return "CWS";
    if (s === "crx")    return "CRX upload";
    if (s === "folder") return "Folder";
    return "Unknown";
  }

  // ─────────────────────────────────────────────────────────────
  // Detail panel
  // ─────────────────────────────────────────────────────────────
  function clearDetail() {
    state.selectedId = null;
    state.detail = null;
    $("#ext-detail-empty").style.display = "";
    const d = $("#ext-detail");
    if (d) { d.style.display = "none"; d.innerHTML = ""; }
  }

  async function renderDetail(id) {
    const empty  = $("#ext-detail-empty");
    const detail = $("#ext-detail");
    if (!detail) return;
    empty.style.display = "none";
    detail.style.display = "";
    detail.innerHTML = `<div class="dense-empty" style="padding: 40px 16px;">Loading…</div>`;

    let x;
    try {
      x = await api(`/api/extensions/${encodeURIComponent(id)}`);
    } catch (e) {
      detail.innerHTML = `<div class="dense-empty" style="padding: 40px 16px; color:#fca5a5;">
        Failed to load: ${escapeHtml(e.message)}
      </div>`;
      return;
    }
    state.detail = x;
    detail.innerHTML = renderDetailMarkup(x);
    wireDetailEvents(x);
  }

  function renderDetailMarkup(x) {
    const profiles = state.profileMap[x.id] || [];
    const isOff    = x.is_enabled === 0 || x.is_enabled === false;
    const icon     = x.icon_b64
      ? `<img class="ext-detail-icon-img" src="${(x.icon_b64.startsWith('data:') ? x.icon_b64 : 'data:image/png;base64,' + x.icon_b64)}" alt="">`
      : `<div class="ext-detail-icon-fallback">🧩</div>`;

    // Permissions summary chips
    const perms = (x.permissions_summary || "").split(",")
      .map(p => p.trim()).filter(Boolean);
    const permsHtml = perms.length
      ? perms.slice(0, 12).map(p => `<span class="ext-perm-chip">${escapeHtml(p)}</span>`).join("")
        + (perms.length > 12 ? `<span class="ext-perm-chip-more">+${perms.length - 12} more</span>` : "")
      : `<span class="muted" style="font-size: 12px;">none requested</span>`;

    // Profile assignment list — checkbox per profile
    const profilesAll = state.profiles || [];
    const assignedSet = new Set(profiles.map(p => p.name));
    const profileRows = profilesAll.length
      ? profilesAll.map(p => `
          <label class="ext-prof-row">
            <input type="checkbox" data-profile="${escapeHtml(p.name)}" ${assignedSet.has(p.name) ? "checked" : ""}>
            <span class="ext-prof-name">${escapeHtml(p.name)}</span>
            ${p.tags?.length ? `<span class="ext-prof-tags">${p.tags.slice(0,3).map(t => `<span class="ext-prof-tag">${escapeHtml(t)}</span>`).join("")}</span>` : ""}
          </label>`).join("")
      : `<div class="muted" style="font-size: 12px;">No profiles exist yet — create one first.</div>`;

    return `
      <div class="ext-detail-header">
        <div class="ext-detail-icon">${icon}</div>
        <div class="ext-detail-meta">
          <div class="ext-detail-name">
            ${escapeHtml(x.name || "(unnamed)")}
            ${isOff ? '<span class="ext-card-off-pill">disabled</span>' : ""}
          </div>
          <div class="ext-detail-sub">
            <span class="ext-source-pill ext-source-${escapeHtml(x.source || "unknown")}">${sourceLabel(x.source)}</span>
            ${x.version ? `<span class="ext-detail-version">v${escapeHtml(x.version)}</span>` : ""}
          </div>
          <div class="ext-detail-id" title="Chrome Extension ID">
            ${escapeHtml(x.id)}
          </div>
        </div>
      </div>

      ${x.description ? `
        <div class="ext-detail-desc">${escapeHtml(x.description)}</div>
      ` : ""}

      <div class="ext-detail-section">
        <div class="ext-detail-section-title">Permissions</div>
        <div class="ext-perm-chips">${permsHtml}</div>
      </div>

      <div class="ext-detail-section">
        <div class="ext-detail-section-title">
          Profiles
          <span class="muted" style="font-weight: normal; font-size: 12px;">
            — auto-installed at launch (data persists per profile)
          </span>
        </div>
        <div class="ext-prof-list" id="ext-prof-list">
          ${profileRows}
        </div>
        ${profilesAll.length > 6 ? `
          <div style="display: flex; gap: 8px; margin-top: 8px;">
            <button class="btn btn-secondary btn-small" id="ext-prof-all">All</button>
            <button class="btn btn-secondary btn-small" id="ext-prof-none">None</button>
            <button class="btn btn-secondary btn-small" id="ext-prof-invert">Invert</button>
          </div>
        ` : ""}
      </div>

      <div class="ext-detail-section">
        <div class="ext-detail-section-title">Settings</div>
        <label class="checkbox-row" style="margin-bottom: 8px;">
          <input type="checkbox" id="ext-toggle-enabled" ${isOff ? "" : "checked"}>
          <span>Enabled in pool
            <span class="muted" style="font-size: 12px;">— uncheck to skip --load-extension even if profiles assigned</span>
          </span>
        </label>
        <label class="checkbox-row" style="margin-bottom: 8px;">
          <input type="checkbox" id="ext-toggle-auto"
                 ${x.auto_install_for_new ? "checked" : ""}>
          <span>Auto-install for new profiles
            <span class="muted" style="font-size: 12px;">— added to every profile created from now on</span>
          </span>
        </label>
      </div>

      <div class="ext-detail-actions">
        <button class="btn btn-secondary btn-small" id="ext-detail-reveal-id"
                title="Copy the extension ID to clipboard">📋 Copy ID</button>
        <button class="btn btn-secondary btn-small" id="ext-detail-test-solo"
                title="Spawn an isolated Chrome with only this extension to verify it loads cleanly. Catches errors the manifest gate doesn't (missing service_worker file, broken default_locale, etc).">
          🧪 Test solo
        </button>
        <div style="flex: 1;"></div>
        <button class="btn btn-danger btn-small" id="ext-detail-remove">
          🗑 Remove from pool
        </button>
      </div>
      <!-- Test result panel — populated by the Test solo handler.
           Hidden until the user clicks Test solo. Shows pass/fail
           verdict + first error/warning + log excerpt. -->
      <div id="ext-detail-solo-result" class="ext-solo-result" style="display: none;"></div>
    `;
  }

  function wireDetailEvents(x) {
    // Per-profile checkbox toggles → fire assign/remove individually.
    // Each checkbox is independent so a single failed call doesn't
    // stall the rest of the user's toggles.
    document.querySelectorAll(".ext-prof-row input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", async () => {
        const pname = cb.dataset.profile;
        cb.disabled = true;
        try {
          if (cb.checked) {
            await api(
              `/api/profiles/${encodeURIComponent(pname)}/extensions`,
              { method: "POST", body: JSON.stringify({ extension_id: x.id, enabled: true }) }
            );
            (state.profileMap[x.id] = state.profileMap[x.id] || []).push({ name: pname, enabled: true });
          } else {
            await api(
              `/api/profiles/${encodeURIComponent(pname)}/extensions/${encodeURIComponent(x.id)}`,
              { method: "DELETE" }
            );
            state.profileMap[x.id] = (state.profileMap[x.id] || []).filter(p => p.name !== pname);
          }
          renderStats();
          renderGrid();
        } catch (e) {
          toast(`Toggle failed: ${e.message}`, true);
          cb.checked = !cb.checked;
        } finally {
          cb.disabled = false;
        }
      });
    });

    // Bulk profile toggles
    $("#ext-prof-all")?.addEventListener("click", () => bulkProfileToggle(x, "all"));
    $("#ext-prof-none")?.addEventListener("click", () => bulkProfileToggle(x, "none"));
    $("#ext-prof-invert")?.addEventListener("click", () => bulkProfileToggle(x, "invert"));

    // Settings toggles
    $("#ext-toggle-enabled")?.addEventListener("change", async (e) => {
      try {
        await api(`/api/extensions/${encodeURIComponent(x.id)}`, {
          method: "PATCH", body: JSON.stringify({ is_enabled: e.target.checked }),
        });
        const row = state.pool.find(r => r.id === x.id);
        if (row) row.is_enabled = e.target.checked ? 1 : 0;
        renderStats();
        renderGrid();
      } catch (err) {
        toast(`Failed: ${err.message}`, true);
        e.target.checked = !e.target.checked;
      }
    });
    $("#ext-toggle-auto")?.addEventListener("change", async (e) => {
      try {
        await api(`/api/extensions/${encodeURIComponent(x.id)}`, {
          method: "PATCH", body: JSON.stringify({ auto_install_for_new: e.target.checked }),
        });
        const row = state.pool.find(r => r.id === x.id);
        if (row) row.auto_install_for_new = e.target.checked ? 1 : 0;
      } catch (err) {
        toast(`Failed: ${err.message}`, true);
        e.target.checked = !e.target.checked;
      }
    });

    // Test solo — isolate this extension in a fresh Chrome to verify
    // it actually loads at runtime. Useful when the manifest gate
    // accepted it but a profile launch keeps failing — narrows the
    // cause from "one of N extensions" to "this specific one".
    $("#ext-detail-test-solo")?.addEventListener("click", () => testSolo(x));

    // Copy ID
    $("#ext-detail-reveal-id")?.addEventListener("click", () => {
      navigator.clipboard?.writeText(x.id).then(
        () => toast("ID copied"),
        () => toast("Copy failed", true)
      );
    });

    // Remove
    $("#ext-detail-remove")?.addEventListener("click", () => removeFromPool(x));
  }

  async function bulkProfileToggle(x, mode) {
    const boxes = document.querySelectorAll(".ext-prof-row input[type=checkbox]");
    const targets = [];
    boxes.forEach(cb => {
      const want = mode === "all"  ? true
                 : mode === "none" ? false
                 : !cb.checked;
      if (cb.checked !== want) targets.push({ cb, want });
    });
    if (!targets.length) return;
    if (targets.length > 5) {
      const ok = await confirmDialog({
        title: "Apply to many profiles?",
        message: `This will toggle ${targets.length} profile assignments. Continue?`,
        confirmText: "Apply",
      });
      if (!ok) return;
    }
    for (const { cb, want } of targets) {
      cb.checked = want;
      cb.dispatchEvent(new Event("change"));
    }
  }

  // ── Solo test — isolate one extension in a fresh Chrome ──────
  // Calls /api/extensions/<id>/test-solo (backend in dashboard/server.py
  // → ghost_shell.extensions.solo_test.test_extension). Renders the
  // verdict inline below the action buttons.
  async function testSolo(x) {
    const btn   = document.getElementById("ext-detail-test-solo");
    const panel = document.getElementById("ext-detail-solo-result");
    if (!btn || !panel) return;

    const origLabel = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "⏳ Testing…";
    panel.style.display = "";
    panel.className = "ext-solo-result is-pending";
    panel.innerHTML = `
      <div class="ext-solo-line">
        Spawning an isolated Chrome with only this extension…
        <span class="muted">(typically 5-8 seconds)</span>
      </div>
    `;

    try {
      // Step 1: enqueue the job. Backend returns 202 + job_id.
      const enq = await api(
        `/api/extensions/${encodeURIComponent(x.id)}/test-solo`,
        { method: "POST", body: JSON.stringify({ timeout: 8 }) }
      );
      const jobId = enq && enq.job_id;
      if (!jobId) {
        throw new Error(enq?.reason || "no job_id returned");
      }

      // Step 2: poll /api/jobs/<id> at 1Hz until done/error.
      // Update the panel periodically so the user sees progress.
      const result = await new Promise((resolve, reject) => {
        const POLL_MS = 1000;
        const MAX_POLLS = 60;   // hard cap = 60s, well past timeout
        let polls = 0;
        const tick = async () => {
          polls += 1;
          try {
            const st = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
            // Update progress text on slow runs
            if (st.status === "queued") {
              const line = panel.querySelector(".ext-solo-line");
              if (line) line.textContent = "Waiting for worker (queue)…";
            } else if (st.status === "running") {
              const line = panel.querySelector(".ext-solo-line");
              if (line) {
                line.innerHTML =
                  `Spawning isolated Chrome with only this extension… ` +
                  `<span class="muted">(${st.elapsed || 0}s)</span>`;
              }
            }
            if (st.status === "done") {
              resolve(st.result);
              return;
            }
            if (st.status === "error") {
              reject(new Error(st.error || "job failed"));
              return;
            }
            if (polls >= MAX_POLLS) {
              reject(new Error("polling timed out"));
              return;
            }
            setTimeout(tick, POLL_MS);
          } catch (e) {
            reject(e);
          }
        };
        setTimeout(tick, POLL_MS);
      });

      // Verdict styling
      const status = result.status || "error";
      const cls    = status === "loads"    ? "is-ok"
                   : status === "warnings" ? "is-warn"
                   : "is-fail";
      const icon   = status === "loads"    ? "✅"
                   : status === "warnings" ? "⚠"
                   : "❌";
      panel.className = `ext-solo-result ${cls}`;

      const errorsHtml = (result.errors || []).length
        ? `<div class="ext-solo-section">
             <div class="ext-solo-section-label">Errors (${result.errors.length})</div>
             <ul class="ext-solo-list">
               ${result.errors.slice(0, 5).map(e =>
                 `<li><code>${escapeHtml(e)}</code></li>`).join("")}
             </ul>
           </div>`
        : "";
      const warningsHtml = (result.warnings || []).length
        ? `<div class="ext-solo-section">
             <div class="ext-solo-section-label">Warnings (${result.warnings.length})</div>
             <ul class="ext-solo-list">
               ${result.warnings.slice(0, 5).map(w =>
                 `<li><code>${escapeHtml(w)}</code></li>`).join("")}
             </ul>
           </div>`
        : "";
      const excerptHtml = result.log_excerpt
        ? `<details class="ext-solo-details">
             <summary>chrome_debug.log tail</summary>
             <pre class="ext-solo-log">${escapeHtml(result.log_excerpt)}</pre>
           </details>`
        : "";

      panel.innerHTML = `
        <div class="ext-solo-verdict">
          <span class="ext-solo-icon">${icon}</span>
          <span class="ext-solo-status">${status.toUpperCase()}</span>
          <span class="ext-solo-duration">${result.duration ?? "?"}s</span>
          ${result.exit_code !== null && result.exit_code !== undefined
            ? `<span class="ext-solo-exit">exit ${result.exit_code}</span>`
            : ""}
        </div>
        <div class="ext-solo-reason">${escapeHtml(result.reason || "")}</div>
        ${errorsHtml}
        ${warningsHtml}
        ${excerptHtml}
      `;

      if (status === "loads") {
        toast("✓ Extension loads cleanly");
      } else if (status === "warnings") {
        toast("⚠ Loads with warnings — see panel", false);
      } else {
        toast("✗ Extension does NOT load — see panel", true);
      }
    } catch (e) {
      panel.className = "ext-solo-result is-fail";
      panel.innerHTML = `
        <div class="ext-solo-verdict">
          <span class="ext-solo-icon">❌</span>
          <span class="ext-solo-status">ERROR</span>
        </div>
        <div class="ext-solo-reason">Test endpoint failed: ${escapeHtml(e.message || String(e))}</div>
      `;
      toast(`Test failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = origLabel;
    }
  }

  async function removeFromPool(x) {
    const usedBy = (state.profileMap[x.id] || []).length;
    const msg = usedBy > 0
      ? `"${x.name}" is currently assigned to ${usedBy} profile(s). Removing will detach it from all of them. Per-profile data inside user-data-dir is NOT deleted.\n\nContinue?`
      : `Remove "${x.name}" from the pool?`;
    const ok = await confirmDialog({
      title: "Remove extension",
      message: msg,
      confirmText: "Remove",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      await api(`/api/extensions/${encodeURIComponent(x.id)}`, { method: "DELETE" });
      toast("Removed");
      state.pool = state.pool.filter(r => r.id !== x.id);
      delete state.profileMap[x.id];
      clearDetail();
      renderStats();
      renderGrid();
    } catch (e) {
      toast(`Remove failed: ${e.message}`, true);
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Add: file upload (CRX or folder.zip)
  // ─────────────────────────────────────────────────────────────
  async function uploadFile(file, kind) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);   // "crx" or "folder"
    try {
      toast(`Uploading ${file.name}…`);
      const r = await fetch("/api/extensions/upload", { method: "POST", body: fd });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: r.statusText }));
        throw new Error(err.error || "upload failed");
      }
      const j = await r.json();
      toast(`✓ ${j.name || "Installed"}`);
      await reloadAll();
      if (j.id) {
        state.selectedId = j.id;
        renderDetail(j.id);
        // Highlight the new card
        const card = document.querySelector(`[data-ext-id="${cssEscape(j.id)}"]`);
        card?.classList.add("selected");
        card?.scrollIntoView({ block: "nearest" });
      }
    } catch (e) {
      toast(`Upload failed: ${e.message}`, true);
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Add: install from CWS by ID
  // ─────────────────────────────────────────────────────────────
  function openCwsIdModal() {
    const m = $("#ext-cws-id-modal");
    if (!m) return;
    $("#ext-cws-id-input").value = "";
    m.style.display = "";
    setTimeout(() => $("#ext-cws-id-input")?.focus(), 50);
  }

  async function installFromCws() {
    const v = ($("#ext-cws-id-input")?.value || "").trim();
    if (!v) { toast("Enter an ID or URL", true); return; }
    const btn = $("#ext-cws-id-install-btn");
    btn.disabled = true;
    btn.textContent = "Downloading…";
    try {
      const j = await api("/api/extensions/install-cws", {
        method: "POST",
        body: JSON.stringify({ id_or_url: v }),
      });
      toast(`✓ ${j.name || "Installed"}`);
      closeModal("ext-cws-id-modal");
      await reloadAll();
      if (j.id) {
        state.selectedId = j.id;
        renderDetail(j.id);
        const card = document.querySelector(`[data-ext-id="${cssEscape(j.id)}"]`);
        card?.classList.add("selected");
        card?.scrollIntoView({ block: "nearest" });
      }
    } catch (e) {
      toast(`Install failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Download & install";
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Add: search the CWS
  // ─────────────────────────────────────────────────────────────
  function openCwsSearchModal() {
    const m = $("#ext-cws-search-modal");
    if (!m) return;
    $("#ext-cws-search-input").value = "";
    m.style.display = "";
    renderRecommended();
    $("#ext-cws-results").innerHTML = `
      <div class="dense-empty" style="padding: 30px 16px; text-align: center;">
        Type a query above to search, or pick a verified extension below.
      </div>`;
    setTimeout(() => $("#ext-cws-search-input")?.focus(), 50);
  }

  function renderRecommended() {
    const wrap = $("#ext-cws-recommended");
    if (!wrap) return;
    wrap.innerHTML = `
      <div class="ext-cws-rec-title">⭐ Verified — install with one click</div>
      <div class="ext-cws-rec-row">
        ${RECOMMENDED.map(r => `
          <button class="ext-cws-rec-card" data-id="${escapeHtml(r.id)}" data-name="${escapeHtml(r.name)}">
            <div class="ext-cws-rec-name">${escapeHtml(r.name)}</div>
            <div class="ext-cws-rec-cat">${escapeHtml(r.category)}</div>
          </button>`).join("")}
      </div>
    `;
    wrap.querySelectorAll(".ext-cws-rec-card").forEach(card => {
      card.addEventListener("click", async () => {
        const id   = card.dataset.id;
        const name = card.dataset.name;
        // If we already have it, don't re-install — just select it
        const have = state.pool.find(p => p.id === id);
        if (have) {
          toast(`${name} is already in the pool`);
          closeModal("ext-cws-search-modal");
          state.selectedId = id;
          renderGrid();
          renderDetail(id);
          return;
        }
        card.disabled = true;
        const orig = card.innerHTML;
        card.innerHTML = `<div class="ext-cws-rec-name">Installing…</div>`;
        try {
          const j = await api("/api/extensions/install-cws", {
            method: "POST", body: JSON.stringify({ id_or_url: id }),
          });
          toast(`✓ ${j.name || name}`);
          closeModal("ext-cws-search-modal");
          await reloadAll();
          if (j.id) {
            state.selectedId = j.id;
            renderDetail(j.id);
          }
        } catch (e) {
          toast(`Install failed: ${e.message}`, true);
          card.innerHTML = orig;
          card.disabled = false;
        }
      });
    });
  }

  async function runCwsSearch() {
    const q = ($("#ext-cws-search-input")?.value || "").trim();
    const out = $("#ext-cws-results");
    if (!q) {
      out.innerHTML = `<div class="dense-empty" style="padding: 16px;">Type a query first.</div>`;
      return;
    }
    out.innerHTML = `<div class="dense-empty" style="padding: 16px;">Searching…</div>`;
    try {
      const j = await api(`/api/extensions/cws-search?q=${encodeURIComponent(q)}`);
      const results = j?.results || [];
      if (!results.length) {
        out.innerHTML = `
          <div class="dense-empty" style="padding: 30px 16px; text-align: center;">
            No results. The CWS scrape is best-effort — try the
            verified-install shortcuts above, or paste an ID directly.
          </div>`;
        return;
      }
      out.innerHTML = results.map(r => `
        <div class="ext-cws-result" data-id="${escapeHtml(r.id)}" data-name="${escapeHtml(r.name || r.slug || "")}">
          <div class="ext-cws-result-icon">🧩</div>
          <div class="ext-cws-result-body">
            <div class="ext-cws-result-name">${escapeHtml(r.name || r.slug || "(no title)")}</div>
            <div class="ext-cws-result-id">${escapeHtml(r.id)}</div>
          </div>
          <button class="btn btn-primary btn-small ext-cws-result-install">Install</button>
        </div>
      `).join("");
      out.querySelectorAll(".ext-cws-result").forEach(row => {
        const btn = row.querySelector(".ext-cws-result-install");
        btn?.addEventListener("click", async () => {
          btn.disabled = true;
          btn.textContent = "…";
          try {
            const id = row.dataset.id;
            const j2 = await api("/api/extensions/install-cws", {
              method: "POST", body: JSON.stringify({ id_or_url: id }),
            });
            toast(`✓ ${j2.name || row.dataset.name || "Installed"}`);
            btn.textContent = "✓";
            await reloadAll();
          } catch (e) {
            toast(`Install failed: ${e.message}`, true);
            btn.disabled = false;
            btn.textContent = "Install";
          }
        });
      });
    } catch (e) {
      out.innerHTML = `<div class="dense-empty" style="padding: 16px; color:#fca5a5;">
        Search failed: ${escapeHtml(e.message)}
      </div>`;
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Helpers
  // ─────────────────────────────────────────────────────────────
  function closeModal(id) {
    const m = document.getElementById(id);
    if (m) m.style.display = "none";
  }

  // CSS.escape polyfill — extension IDs are 32 lowercase letters so
  // the input is safe, but keep this for defence in depth.
  function cssEscape(s) {
    if (window.CSS?.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
  }

  return { init, teardown };
})();
