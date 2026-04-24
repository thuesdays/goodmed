// ═══════════════════════════════════════════════════════════════
// fingerprint.js — Fingerprint coherence page (Phase 2)
//
// Renders the current fingerprint, its coherence score, the list
// of validator checks, an editable field table with per-field locks,
// a template picker, a history panel, and a live self-test runner.
//
// API contracts — see dashboard_server.py §FINGERPRINT COHERENCE SYSTEM:
//   GET    /api/fingerprint/templates
//   GET    /api/fingerprint/<profile>
//   POST   /api/fingerprint/<profile>/generate   { template_id, locked_fields, mode, reason }
//   PUT    /api/fingerprint/<profile>            { patches: { "field.path": value } }
//   POST   /api/fingerprint/<profile>/validate
//   POST   /api/fingerprint/<profile>/selftest
//   GET    /api/fingerprint/<profile>/history?limit=N
//   POST   /api/fingerprint/<profile>/activate/<fp_id>
//   DELETE /api/fingerprint/entry/<fp_id>
//   GET    /api/fingerprints/summary
// ═══════════════════════════════════════════════════════════════

const FingerprintPage = (() => {

  const state = {
    profiles:         [],
    currentProfile:   null,
    templates:        [],
    selectedTemplate: null,
    categoryFilter:   "",     // "" = all; "desktop" | "laptop" | "phone"
    fingerprint:      null,
    locks:            new Set(),
    edits:            {},
    history:          [],
    selftest:         null,
    currentTab:       "checks",
  };

  const API = {
    listProfiles:   ()           => api("/api/profiles"),
    listTemplates:  ()           => api("/api/fingerprint/templates"),
    getCurrent:     (p)          => api(`/api/fingerprint/${encodeURIComponent(p)}`),
    generate:       (p, body)    => api(`/api/fingerprint/${encodeURIComponent(p)}/generate`,
                                        { method: "POST", body: JSON.stringify(body) }),
    patch:          (p, patches) => api(`/api/fingerprint/${encodeURIComponent(p)}`,
                                        { method: "PUT", body: JSON.stringify({ patches }) }),
    revalidate:     (p)          => api(`/api/fingerprint/${encodeURIComponent(p)}/validate`,
                                        { method: "POST" }),
    selftest:       (p)          => api(`/api/fingerprint/${encodeURIComponent(p)}/selftest`,
                                        { method: "POST" }),
    listHistory:    (p, limit=30) => api(`/api/fingerprint/${encodeURIComponent(p)}/history?limit=${limit}`),
    activate:       (p, fpId)    => api(`/api/fingerprint/${encodeURIComponent(p)}/activate/${fpId}`,
                                        { method: "POST" }),
    deleteEntry:    (fpId)       => api(`/api/fingerprint/entry/${fpId}`,
                                        { method: "DELETE" }),
  };

  // Ordered list of fields shown in the editor table. Each entry has
  // a dot-path (matches the patch API) and a friendly label. Keeping
  // this as data lets us add fields without touching the renderer.
  const FIELD_REGISTRY = [
    { path: "user_agent",          label: "User-Agent",          kind: "text" },
    { path: "platform",            label: "navigator.platform",  kind: "text" },
    { path: "vendor",              label: "navigator.vendor",    kind: "text" },
    { path: "chrome_full_version", label: "Chrome version",      kind: "text" },
    { path: "language",            label: "Primary language",    kind: "text" },
    { path: "languages",           label: "Languages list",      kind: "json" },
    { path: "timezone",            label: "Timezone",            kind: "text" },
    { path: "hardware_concurrency",label: "CPU cores",           kind: "int"  },
    { path: "device_memory",       label: "Device memory (GB)",  kind: "int"  },
    { path: "max_touch_points",    label: "Max touch points",    kind: "int"  },
    { path: "screen.width",        label: "Screen width",        kind: "int"  },
    { path: "screen.height",       label: "Screen height",       kind: "int"  },
    { path: "dpr",                 label: "Device pixel ratio",  kind: "float"},
    { path: "webgl.vendor",        label: "WebGL vendor",        kind: "text" },
    { path: "webgl.renderer",      label: "WebGL renderer",      kind: "text" },
    { path: "audio_sample_rate",   label: "Audio sample rate",   kind: "int"  },
    { path: "webdriver",           label: "webdriver flag",      kind: "bool" },
    { path: "fonts",               label: "Fonts",               kind: "json" },
  ];

  function getPath(obj, path) {
    if (!obj) return undefined;
    const parts = String(path).split(".");
    let cur = obj;
    for (const k of parts) {
      if (cur == null) return undefined;
      cur = cur[k];
    }
    return cur;
  }

  function gradeClass(grade) {
    if (!grade) return "fp-score-unknown";
    return `fp-score-${grade}`;
  }

  function fmtValue(value, kind) {
    if (value == null) return '<span class="muted">—</span>';
    if (kind === "bool") return value ? "true" : "false";
    if (kind === "json") {
      if (Array.isArray(value)) {
        if (value.length <= 5) return escapeHtml(JSON.stringify(value));
        return `<span title="${escapeHtml(JSON.stringify(value))}">` +
               `[${value.length} items] ${escapeHtml(value.slice(0, 3).join(", "))}…</span>`;
      }
      return escapeHtml(JSON.stringify(value));
    }
    return escapeHtml(String(value));
  }

  function parseInput(raw, kind) {
    if (raw == null) return null;
    if (kind === "int") {
      const n = parseInt(raw, 10);
      if (isNaN(n)) throw new Error("expected an integer");
      return n;
    }
    if (kind === "float") {
      const n = parseFloat(raw);
      if (isNaN(n)) throw new Error("expected a number");
      return n;
    }
    if (kind === "bool") {
      if (raw === "true" || raw === true)  return true;
      if (raw === "false" || raw === false) return false;
      throw new Error("expected true/false");
    }
    if (kind === "json") {
      return JSON.parse(raw);
    }
    return String(raw);
  }

  async function init() {
    bindEvents();

    try {
      const [profilesResp, tmplResp] = await Promise.all([
        API.listProfiles(),
        API.listTemplates(),
      ]);
      state.profiles  = (profilesResp.profiles || profilesResp || []).map(p => p.name || p);
      state.templates = tmplResp.templates || [];
    } catch (e) {
      toast("Failed to load profiles/templates: " + e.message, true);
      return;
    }

    populateProfileSelector();
    renderTemplateList();

    // Preselect via ?profile=… in the hash, else first profile.
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const pre = params.get("profile") || state.profiles[0] || null;
    if (pre) {
      $("#fp-profile-selector").value = pre;
      await selectProfile(pre);
    }
  }

  function bindEvents() {
    $("#fp-profile-selector").addEventListener("change", (e) => {
      selectProfile(e.target.value);
    });

    // "← Back to profile" — returns to the profile detail page pinned
    // to whichever profile is currently selected in the fp editor.
    // We stash the profile name in configCache so profile-detail.js
    // picks it up on init (it reads configCache.browser.profile_name).
    // Category filter — re-renders template list with a narrower subset
    document.addEventListener("click", (e) => {
      const b = e.target.closest(".fp-category-btn");
      if (!b) return;
      $$(".fp-category-btn").forEach(x => x.classList.toggle("active", x === b));
      state.categoryFilter = b.dataset.cat || "";
      renderTemplateList();
    });

    $("#fp-back-to-profile-btn")?.addEventListener("click", () => {
      if (state.currentProfile && typeof configCache === "object" && configCache?.browser) {
        configCache.browser.profile_name = state.currentProfile;
      }
      navigate("profile");
    });

    $("#fp-regen-full-btn").addEventListener("click", () => openRegenModal("full"));
    $("#fp-reshuffle-btn").addEventListener("click", () => openRegenModal("reshuffle"));
    $("#fp-selftest-btn").addEventListener("click", runSelftest);
    $("#fp-revalidate-btn").addEventListener("click", revalidate);
    $("#fp-mode-toggle-btn")?.addEventListener("click", toggleMode);
    $("#fp-create-btn")?.addEventListener("click", () => {
      doGenerate({ mode: "full", template_id: null,
                   locked_fields: {}, reason: "initial generation" });
    });

    $("#fp-regen-confirm-btn").addEventListener("click", confirmRegen);
    $("#fp-apply-template-btn").addEventListener("click", applySelectedTemplate);

    $("#fp-fields-save-btn").addEventListener("click", saveFieldEdits);
    $("#fp-fields-reset-btn").addEventListener("click", resetFieldEdits);

    $("#fp-tabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".fp-tab");
      if (!tab) return;
      switchTab(tab.dataset.tab);
    });

    document.addEventListener("click", (e) => {
      const closer = e.target.closest("[data-close]");
      if (!closer) return;
      const m = document.getElementById(closer.dataset.close);
      if (m) m.style.display = "none";
    });
  }

  function populateProfileSelector() {
    const sel = $("#fp-profile-selector");
    sel.innerHTML = "";
    if (!state.profiles.length) {
      sel.innerHTML = '<option value="">— no profiles —</option>';
      return;
    }
    for (const name of state.profiles) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
  }

  async function selectProfile(name) {
    if (!name) return;
    state.currentProfile = name;
    state.edits = {};
    state.locks = new Set();
    state.selftest = null;

    try {
      const [cur, hist] = await Promise.all([
        API.getCurrent(name),
        API.listHistory(name, 30),
      ]);
      state.fingerprint = cur.fingerprint || null;
      state.history     = hist.history || [];

      if (state.fingerprint?.locked_fields) {
        state.locks = new Set(state.fingerprint.locked_fields);
      }

      const tid = state.fingerprint?.payload?.template_id;
      state.selectedTemplate = tid || null;
    } catch (e) {
      toast("Failed to load fingerprint: " + e.message, true);
      state.fingerprint = null;
      state.history = [];
    }

    renderAll();
  }

  function renderAll() {
    renderHeaderStrip();
    renderModeToggle();
    renderEmptyState();
    renderTemplateList();
    renderChecks();
    renderFields();
    renderHistory();
    renderSelftest();
    renderTabBadges();
  }

  function renderHeaderStrip() {
    const fp  = state.fingerprint;
    const box = $("#fp-score-box");
    const val = $("#fp-score-value");
    const lbl = $("#fp-score-label");
    const sum = $("#fp-summary-line");
    const meta = $("#fp-summary-meta");

    box.classList.remove(
      "fp-score-excellent", "fp-score-good",
      "fp-score-warning", "fp-score-critical", "fp-score-unknown"
    );

    if (!fp) {
      val.textContent = "—";
      lbl.textContent = "no data";
      box.classList.add("fp-score-unknown");
      sum.textContent = state.currentProfile
        ? `${state.currentProfile} has no fingerprint yet.`
        : "Select a profile to view its fingerprint.";
      meta.textContent = "";
      return;
    }

    const rep   = fp.coherence_report || {};
    const score = (fp.coherence_score ?? rep.score);
    const grade = rep.grade || "unknown";

    val.textContent = score == null ? "—" : String(score);
    lbl.textContent = grade;
    box.classList.add(gradeClass(grade));

    sum.textContent = rep.summary
      || `${fp.template_name || fp.template_id || "unknown template"}`;

    const parts = [];
    if (fp.template_name || fp.template_id) {
      parts.push(`📦 ${fp.template_name || fp.template_id}`);
    }
    if (fp.source) parts.push(`source: ${fp.source}`);
    if (fp.timestamp) parts.push(`generated ${timeAgo(fp.timestamp)}`);
    if (state.locks.size) parts.push(`🔒 ${state.locks.size} locked`);
    meta.innerHTML = parts.map(escapeHtml).join(" · ");
  }

  // Show the mode-toggle button with a label reflecting the CURRENT mode.
  // Hidden for profiles without a fingerprint yet — switching without a
  // baseline is confusing.
  function renderModeToggle() {
    const btn = $("#fp-mode-toggle-btn");
    if (!btn) return;
    const fp = state.fingerprint;
    if (!fp || !fp.payload) { btn.style.display = "none"; return; }
    const tpl = state.templates.find(t => t.id === fp.payload.template_id);
    const isMobile = !!(tpl && tpl.is_mobile);
    btn.style.display = "inline-flex";
    btn.textContent = isMobile ? "↔ Switch to desktop" : "↔ Switch to mobile";
    btn.dataset.nextMode = isMobile ? "desktop" : "mobile";
  }

  async function toggleMode() {
    const btn = $("#fp-mode-toggle-btn");
    const nextMode = btn?.dataset?.nextMode;
    if (!state.currentProfile || !nextMode) return;
    const label = nextMode === "mobile" ? "mobile" : "desktop";
    btn.disabled = true;
    btn.textContent = `⏳ Switching to ${label}…`;
    try {
      const resp = await api(
        `/api/fingerprint/${encodeURIComponent(state.currentProfile)}/mode`,
        { method: "POST", body: JSON.stringify({ mode: nextMode }) }
      );
      toast(resp.source === "generated"
        ? `✓ Generated fresh ${label} fingerprint`
        : `✓ Switched to existing ${label} fingerprint from history`);
      await selectProfile(state.currentProfile);
    } catch (e) {
      toast("Switch failed: " + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  function renderEmptyState() {
    const hasFP = !!state.fingerprint;
    $("#fp-empty-state").style.display = hasFP ? "none" : "flex";
    $("#fp-layout").style.display      = hasFP ? "grid" : "none";
    $("#fp-reshuffle-btn").disabled  = !hasFP;
    $("#fp-selftest-btn").disabled   = !hasFP;
    $("#fp-revalidate-btn").disabled = !hasFP;
  }

  function renderTemplateList() {
    const list = $("#fp-template-list");
    const countEl = $("#fp-template-count");
    if (!list) return;

    if (!state.templates.length) {
      list.innerHTML = '<div class="dense-empty">No templates loaded.</div>';
      countEl.textContent = "0";
      return;
    }

    countEl.textContent = `${state.templates.length} templates`;

    const filtered = state.categoryFilter
      ? state.templates.filter(t => t.category === state.categoryFilter)
      : state.templates;
    const sorted = [...filtered].sort(
      (a, b) => (b.market_share_pct || 0) - (a.market_share_pct || 0)
    );

    if (!sorted.length) {
      list.innerHTML = `<div class="dense-empty">
        No templates in the ${escapeHtml(state.categoryFilter || "filtered")} category.
      </div>`;
      return;
    }
    list.innerHTML = sorted.map(t => {
      const selected = state.selectedTemplate === t.id;
      const current  = state.fingerprint?.payload?.template_id === t.id;
      const mobileTag = t.is_mobile
        ? '<span class="fp-template-mobile-badge">MOBILE</span>' : "";
      return `
        <div class="fp-template-row ${selected ? "selected" : ""} ${current ? "current" : ""}"
             data-template-id="${escapeHtml(t.id)}">
          <div class="fp-template-label">
            ${escapeHtml(t.label)}
            ${current ? '<span class="fp-template-current-badge">current</span>' : ""}
            ${mobileTag}
          </div>
          <div class="fp-template-meta">
            <span class="fp-template-os">${escapeHtml(t.os)}</span>
            <span class="fp-template-gpu">${escapeHtml(t.gpu_vendor)}</span>
            <span class="fp-template-share">${Number(t.market_share_pct || 0).toFixed(1)}%</span>
          </div>
        </div>
      `;
    }).join("");

    list.querySelectorAll(".fp-template-row").forEach(row => {
      row.addEventListener("click", () => {
        state.selectedTemplate = row.dataset.templateId;
        list.querySelectorAll(".fp-template-row.selected")
            .forEach(r => r.classList.remove("selected"));
        row.classList.add("selected");
        const current = state.fingerprint?.payload?.template_id;
        $("#fp-apply-template-btn").disabled =
          !state.selectedTemplate || state.selectedTemplate === current;
      });
    });

    const current = state.fingerprint?.payload?.template_id;
    $("#fp-apply-template-btn").disabled =
      !state.selectedTemplate || state.selectedTemplate === current;
  }

  function renderChecks() {
    const host = $("#fp-check-list");
    const summary = $("#fp-checks-summary");
    if (!host) return;

    const rep = state.fingerprint?.coherence_report;
    if (!rep) {
      host.innerHTML = '<div class="dense-empty">No validation data yet.</div>';
      summary.textContent = "";
      return;
    }

    const checks = rep.checks || [];
    const groups = { critical: [], important: [], warning: [] };
    for (const c of checks) {
      const cat = c.category || "warning";
      (groups[cat] || (groups[cat] = [])).push(c);
    }

    const statusCount = checks.reduce((acc, c) => {
      acc[c.status] = (acc[c.status] || 0) + 1;
      return acc;
    }, {});
    summary.textContent =
      `${statusCount.pass || 0} pass · ${statusCount.warn || 0} warn · ${statusCount.fail || 0} fail · ${statusCount.skip || 0} skipped`;

    const section = (title, rows) => {
      if (!rows.length) return "";
      return `
        <div class="fp-check-group">
          <div class="fp-check-group-header">${escapeHtml(title)}
            <span class="fp-check-group-count">${rows.length}</span>
          </div>
          ${rows.map(c => `
            <div class="fp-check-row fp-check-${c.status}">
              <span class="fp-check-status-dot"></span>
              <div class="fp-check-body">
                <div class="fp-check-name">${escapeHtml(c.name)}</div>
                <div class="fp-check-detail">${escapeHtml(c.detail || "")}</div>
              </div>
              <span class="fp-check-status-label">${escapeHtml(c.status)}</span>
            </div>
          `).join("")}
        </div>
      `;
    };

    host.innerHTML =
        section("Critical",  groups.critical)
      + section("Important", groups.important)
      + section("Warnings",  groups.warning);
  }

  function renderFields() {
    const tbody = $("#fp-field-tbody");
    const fp = state.fingerprint?.payload;
    if (!fp) {
      tbody.innerHTML =
        '<tr><td colspan="4" class="dense-empty-cell">No fingerprint loaded.</td></tr>';
      $("#fp-fields-save-btn").disabled = true;
      return;
    }

    tbody.innerHTML = FIELD_REGISTRY.map(f => {
      const originalValue = getPath(fp, f.path);
      const edited = Object.prototype.hasOwnProperty.call(state.edits, f.path);
      const value = edited ? state.edits[f.path] : originalValue;
      const locked = state.locks.has(f.path);

      return `
        <tr class="fp-field-row ${edited ? "edited" : ""}" data-path="${escapeHtml(f.path)}">
          <td class="col-lock">
            <button class="fp-lock-btn ${locked ? "locked" : ""}"
                    data-lock="${escapeHtml(f.path)}"
                    title="${locked ? "Unlock (regeneration may change this)" : "Lock (regeneration preserves this)"}">
              ${locked ? "🔒" : "🔓"}
            </button>
          </td>
          <td class="col-field">
            <div class="fp-field-label">${escapeHtml(f.label)}</div>
            <div class="fp-field-path">${escapeHtml(f.path)}</div>
          </td>
          <td class="col-value">
            <div class="fp-field-value"
                 data-edit="${escapeHtml(f.path)}"
                 data-kind="${escapeHtml(f.kind)}">
              ${fmtValue(value, f.kind)}
            </div>
          </td>
          <td class="col-meta muted">${f.kind}</td>
        </tr>
      `;
    }).join("");

    tbody.querySelectorAll(".fp-lock-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const path = btn.dataset.lock;
        if (state.locks.has(path)) state.locks.delete(path);
        else state.locks.add(path);
        renderFields();
        renderHeaderStrip();
      });
    });

    tbody.querySelectorAll(".fp-field-value").forEach(cell => {
      cell.addEventListener("dblclick", () => startFieldEdit(cell));
    });

    $("#fp-fields-save-btn").disabled = !Object.keys(state.edits).length;
  }

  function startFieldEdit(cell) {
    const path = cell.dataset.edit;
    const kind = cell.dataset.kind;
    const fp = state.fingerprint?.payload;
    if (!fp) return;
    const current = Object.prototype.hasOwnProperty.call(state.edits, path)
      ? state.edits[path]
      : getPath(fp, path);

    const raw = kind === "json" ? JSON.stringify(current) : String(current ?? "");

    if (kind === "bool") {
      cell.innerHTML = `
        <select class="fp-field-input">
          <option value="true"  ${current === true  ? "selected" : ""}>true</option>
          <option value="false" ${current === false ? "selected" : ""}>false</option>
        </select>
      `;
    } else {
      cell.innerHTML = `
        <input type="text" class="fp-field-input" value="${escapeHtml(raw)}"
               spellcheck="false">
      `;
    }

    const input = cell.querySelector(".fp-field-input");
    input.focus();
    if (input.select) input.select();

    const commit = () => {
      let val;
      try {
        val = parseInput(input.value ?? input.checked, kind);
      } catch (e) {
        toast(`Invalid value for ${path}: ${e.message}`, true);
        renderFields();
        return;
      }
      const orig = getPath(fp, path);
      if (JSON.stringify(val) === JSON.stringify(orig)) {
        delete state.edits[path];
      } else {
        state.edits[path] = val;
      }
      renderFields();
    };
    const cancel = () => { renderFields(); };

    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") cancel();
    });
  }

  async function saveFieldEdits() {
    if (!state.currentProfile || !Object.keys(state.edits).length) return;
    const btn = $("#fp-fields-save-btn");
    btn.disabled = true;
    try {
      const resp = await API.patch(state.currentProfile, state.edits);
      toast(`Saved ${Object.keys(state.edits).length} field(s). Score: ${resp.validation?.score ?? "—"}/100`);
      state.edits = {};
      await selectProfile(state.currentProfile);
    } catch (e) {
      toast("Save failed: " + e.message, true);
      btn.disabled = false;
    }
  }

  function resetFieldEdits() {
    if (!Object.keys(state.edits).length) return;
    state.edits = {};
    renderFields();
  }

  function renderHistory() {
    const tbody = $("#fp-history-tbody");
    if (!state.history.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="dense-empty-cell">No history yet.</td></tr>';
      return;
    }

    tbody.innerHTML = state.history.map(h => {
      const isCurrent = !!h.is_current;
      const scoreClass =
        h.coherence_score == null ? "muted" :
        h.coherence_score >= 90   ? "stat-ok" :
        h.coherence_score >= 75   ? ""        :
        "stat-err";
      return `
        <tr class="fp-history-row ${isCurrent ? "current" : ""}">
          <td>${fmtTimestamp(h.timestamp)}</td>
          <td>${escapeHtml(h.template_name || h.template_id || "—")}</td>
          <td class="num ${scoreClass}">${h.coherence_score ?? "—"}</td>
          <td class="muted">${escapeHtml(h.source || "—")}</td>
          <td class="muted">${escapeHtml(h.reason || "—")}</td>
          <td>
            ${isCurrent
              ? '<span class="fp-history-current-tag">current</span>'
              : `
                <button class="btn btn-secondary btn-small"
                        data-activate="${h.id}">Restore</button>
                <button class="btn btn-secondary btn-small btn-danger"
                        data-delete="${h.id}">Delete</button>
              `}
          </td>
        </tr>
      `;
    }).join("");

    tbody.querySelectorAll("[data-activate]").forEach(btn => {
      btn.addEventListener("click", () => activateHistory(btn.dataset.activate));
    });
    tbody.querySelectorAll("[data-delete]").forEach(btn => {
      btn.addEventListener("click", () => deleteHistory(btn.dataset.delete));
    });
  }

  async function activateHistory(fpId) {
    if (!await confirmDialog({
      title: "Restore fingerprint?",
      message: "This will make the selected snapshot the active fingerprint for this profile. The current one will stay in history.",
      confirmText: "Restore",
    })) return;
    try {
      await API.activate(state.currentProfile, fpId);
      toast("Fingerprint restored");
      await selectProfile(state.currentProfile);
    } catch (e) {
      toast("Restore failed: " + e.message, true);
    }
  }

  async function deleteHistory(fpId) {
    if (!await confirmDialog({
      title: "Delete snapshot?",
      message: "The snapshot will be permanently removed from history. The current fingerprint is not affected.",
      confirmText: "Delete",
      confirmStyle: "danger",
    })) return;
    try {
      await API.deleteEntry(fpId);
      toast("Snapshot deleted");
      await selectProfile(state.currentProfile);
    } catch (e) {
      toast("Delete failed: " + e.message, true);
    }
  }

  async function runSelftest() {
    if (!state.currentProfile) return;
    const btn = $("#fp-selftest-btn");
    const body = $("#fp-selftest-body");
    switchTab("selftest");

    btn.disabled = true;
    btn.textContent = "⏳ Running…";
    body.innerHTML = `
      <div class="fp-selftest-running">
        <div class="spinner-sm"></div>
        <div>Launching browser and probing fingerprint… (5-15s)</div>
      </div>
    `;

    try {
      const report = await API.selftest(state.currentProfile);
      state.selftest = report;
      renderSelftest();
      toast(`Self-test complete (${Math.round(report.duration_ms || 0)}ms)`);
    } catch (e) {
      body.innerHTML = `
        <div class="fp-selftest-error">
          Self-test failed: ${escapeHtml(e.message)}
        </div>
      `;
      toast("Self-test failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "🔬 Run self-test";
    }
  }

  function renderSelftest() {
    const body = $("#fp-selftest-body");
    const meta = $("#fp-selftest-meta");
    const r = state.selftest;
    if (!r) { meta.textContent = ""; return; }

    const cmp = r.comparison || {};
    const mismatches = cmp.mismatches || [];
    const coh = r.coherence || {};

    meta.textContent =
      `ran in ${Math.round(r.duration_ms || 0)}ms · ` +
      `${mismatches.length} mismatch${mismatches.length === 1 ? "" : "es"}`;

    const mismatchHtml = mismatches.length === 0
      ? `<div class="fp-selftest-ok">✓ All configured values match Chrome's runtime report. Stealth patches are working.</div>`
      : mismatches.map(m => `
          <div class="fp-selftest-mismatch sev-${escapeHtml(m.severity || "warning")}">
            <div class="fp-selftest-mismatch-label">${escapeHtml(m.label)}</div>
            <div class="fp-selftest-mismatch-values">
              <div>
                <span class="fp-selftest-tag">configured</span>
                <code>${escapeHtml(JSON.stringify(m.configured))}</code>
              </div>
              <div>
                <span class="fp-selftest-tag">actual</span>
                <code>${escapeHtml(JSON.stringify(m.actual))}</code>
              </div>
            </div>
          </div>
        `).join("");

    body.innerHTML = `
      <div class="fp-selftest-summary">
        <div class="fp-selftest-coh">
          <div class="fp-selftest-coh-label">Runtime coherence</div>
          <div class="fp-selftest-coh-value ${gradeClass(coh.grade)}">${coh.score ?? "—"}/100</div>
          <div class="fp-selftest-coh-summary muted">${escapeHtml(coh.summary || "—")}</div>
        </div>
        <div class="fp-selftest-checks">
          ${mismatchHtml}
        </div>
      </div>
    `;
  }

  function openRegenModal(prefill) {
    const m = $("#fp-regen-modal");
    m.querySelector(`input[name="fp-regen-mode"][value="${prefill}"]`).checked = true;
    $("#fp-regen-reason").value = "";
    m.style.display = "flex";
  }

  async function confirmRegen() {
    const mode = document.querySelector('input[name="fp-regen-mode"]:checked')?.value || "full";
    const reason = $("#fp-regen-reason").value.trim();
    $("#fp-regen-modal").style.display = "none";

    const template_id = (mode === "template_only" || mode === "full")
      ? state.selectedTemplate
      : null;

    if (mode === "template_only" && !template_id) {
      toast("Pick a template in the left sidebar first", true);
      return;
    }

    const locked_fields = {};
    if (mode !== "full" && state.fingerprint?.payload) {
      for (const p of state.locks) {
        locked_fields[p] = getPath(state.fingerprint.payload, p);
      }
    }

    await doGenerate({ mode, template_id, locked_fields,
                       reason: reason || `user requested ${mode}` });
  }

  async function doGenerate(body) {
    if (!state.currentProfile) return;
    try {
      const resp = await API.generate(state.currentProfile, body);
      toast(`Regenerated · score ${resp.validation?.score ?? "—"}/100`);
      await selectProfile(state.currentProfile);
    } catch (e) {
      toast("Generate failed: " + e.message, true);
    }
  }

  async function applySelectedTemplate() {
    if (!state.selectedTemplate) return;
    const locked_fields = {};
    if (state.fingerprint?.payload) {
      for (const p of state.locks) {
        locked_fields[p] = getPath(state.fingerprint.payload, p);
      }
    }
    await doGenerate({
      mode: "template_only",
      template_id: state.selectedTemplate,
      locked_fields,
      reason: `switched template → ${state.selectedTemplate}`,
    });
  }

  async function revalidate() {
    if (!state.currentProfile) return;
    try {
      const r = await API.revalidate(state.currentProfile);
      await selectProfile(state.currentProfile);
      toast(`Re-validated · score ${r.validation?.score ?? "—"}/100`);
    } catch (e) {
      toast("Re-validate failed: " + e.message, true);
    }
  }

  function switchTab(name) {
    state.currentTab = name;
    $$(".fp-tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    $$(".fp-tabpane").forEach(p => p.classList.toggle("active", p.dataset.tabpane === name));
  }

  function renderTabBadges() {
    const rep = state.fingerprint?.coherence_report;
    if (rep) {
      const checks = rep.checks || [];
      const fails = checks.filter(c => c.status === "fail").length;
      const warns = checks.filter(c => c.status === "warn").length;
      const b = $("#fp-tab-checks-badge");
      b.textContent = fails ? `${fails} fail` : warns ? `${warns} warn` : "ok";
      b.className = "fp-tab-badge " + (fails ? "bad" : warns ? "warn" : "ok");
    } else {
      $("#fp-tab-checks-badge").textContent = "—";
    }

    const edited = Object.keys(state.edits).length;
    const fb = $("#fp-tab-fields-badge");
    fb.textContent = edited ? `${edited} pending` : FIELD_REGISTRY.length;
    fb.className = "fp-tab-badge " + (edited ? "warn" : "");

    $("#fp-tab-history-badge").textContent = state.history.length || "—";
    $("#fp-tab-selftest-badge").classList.toggle("fp-tab-badge-hidden", !state.selftest);
    if (state.selftest) {
      const mm = state.selftest.comparison?.mismatches?.length || 0;
      const b = $("#fp-tab-selftest-badge");
      b.textContent = mm ? `${mm} diff` : "ok";
      b.className = "fp-tab-badge " + (mm ? "bad" : "ok");
    }
  }

  return { init };
})();
