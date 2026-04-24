// ═══════════════════════════════════════════════════════════════
// accounts.js — Vault / credential manager page.
//
// Three UI states driven by /api/vault/status:
//   A. not initialized  → setup form
//   B. locked           → unlock form
//   C. unlocked         → main manager: filters + table + add/edit modal
//
// Backend endpoints (see server.py):
//   GET  /api/vault/status
//   POST /api/vault/initialize  { master_password }
//   POST /api/vault/unlock      { master_password }
//   POST /api/vault/lock
//   POST /api/vault/reset       { master_password }
//   GET  /api/vault/kinds
//   GET  /api/vault/items?kind=&service=&status=&q=
//   POST /api/vault/items
//   GET  /api/vault/items/<id>
//   PUT  /api/vault/items/<id>
//   DELETE /api/vault/items/<id>
//   GET  /api/vault/items/<id>/totp
// ═══════════════════════════════════════════════════════════════

const VaultPage = (() => {

  const state = {
    status:       null,       // { initialized, unlocked }
    kinds:        {},         // { account: {...}, ... }
    items:        [],
    summary:      { by_kind: {}, by_status: {} },
    filterKind:   "",
    filterSearch: "",
    searchTimer:  null,
    editing:      null,       // edit mode: item id; null = create
    editKind:     "account",
    customFields: [],         // for kind=custom: [{key, value}]
    profiles:     [],
  };

  async function init() {
    bindEvents();
    await reloadAll();
  }

  // ─────────────────────────────────────────────────────────────
  // State machine
  // ─────────────────────────────────────────────────────────────
  async function reloadAll() {
    try {
      state.status = await api("/api/vault/status");
    } catch (e) {
      state.status = { initialized: false, unlocked: false };
    }
    renderState();
  }

  function renderState() {
    const setup   = $("#vault-setup");
    const unlock  = $("#vault-unlock");
    const manager = $("#vault-manager");
    const headerActions = $("#vault-header-actions");

    setup.style.display   = "none";
    unlock.style.display  = "none";
    manager.style.display = "none";
    headerActions.style.display = "none";

    const s = state.status || {};
    if (!s.initialized) {
      setup.style.display = "flex";
    } else if (!s.unlocked) {
      unlock.style.display = "flex";
    } else {
      manager.style.display = "block";
      headerActions.style.display = "flex";
      loadManager();
    }
  }

  function bindEvents() {
    // Setup form
    $("#vault-setup-submit").addEventListener("click", submitSetup);
    $("#vault-setup-pw2")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitSetup(); }
    });

    // Unlock form
    $("#vault-unlock-form").addEventListener("submit", (e) => {
      e.preventDefault(); submitUnlock();
    });
    $("#vault-reset-btn").addEventListener("click", resetVault);

    // Lock button
    $("#vault-lock-btn").addEventListener("click", lockVault);

    // Filters
    $("#vault-search").addEventListener("input", (e) => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.filterSearch = (e.target.value || "").trim();
        loadItems();
      }, 300);
    });
    $("#vault-kind-filter").addEventListener("click", (e) => {
      const b = e.target.closest("[data-kind]");
      if (!b) return;
      $$("#vault-kind-filter button").forEach(x => x.classList.toggle("active", x === b));
      state.filterKind = b.dataset.kind;
      loadItems();
    });

    // Add / edit
    $("#vault-add-btn").addEventListener("click", () => openEditor(null));
    $("#vault-edit-save").addEventListener("click", saveItem);

    // Custom-kind field editor
    $("#vault-f-custom-add").addEventListener("click", () => {
      state.customFields.push({key: "", value: ""});
      renderCustomFields();
    });

    // Modal closers
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-close]");
      if (!t) return;
      const m = document.getElementById(t.dataset.close);
      if (m) m.style.display = "none";
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Setup / unlock / lock
  // ─────────────────────────────────────────────────────────────
  async function submitSetup() {
    const pw1 = $("#vault-setup-pw1").value;
    const pw2 = $("#vault-setup-pw2").value;
    const err = $("#vault-setup-error");
    err.style.display = "none";
    if (!pw1 || pw1.length < 4) {
      err.textContent = "Master password must be at least 4 characters.";
      err.style.display = "block"; return;
    }
    if (pw1 !== pw2) {
      err.textContent = "Passwords don't match.";
      err.style.display = "block"; return;
    }
    try {
      await api("/api/vault/initialize",
                { method: "POST", body: JSON.stringify({ master_password: pw1 }) });
      toast("✓ Vault initialized");
      await reloadAll();
    } catch (e) {
      err.textContent = e.message; err.style.display = "block";
    }
  }

  async function submitUnlock() {
    const pw  = $("#vault-unlock-pw").value;
    const err = $("#vault-unlock-error");
    err.style.display = "none";
    try {
      await api("/api/vault/unlock",
                { method: "POST", body: JSON.stringify({ master_password: pw }) });
      toast("✓ Vault unlocked");
      $("#vault-unlock-pw").value = "";
      await reloadAll();
    } catch (e) {
      err.textContent = e.message; err.style.display = "block";
    }
  }

  async function lockVault() {
    try {
      await api("/api/vault/lock", { method: "POST" });
      toast("✓ Vault locked");
      await reloadAll();
    } catch (e) { toast("Lock failed: " + e.message, true); }
  }

  async function resetVault() {
    if (!await confirmDialog({
      title: "Reset vault?",
      message: "This permanently deletes ALL encrypted secrets. You will lose every password, key, and note stored here. This cannot be undone.",
      confirmText: "Yes, wipe everything",
      confirmStyle: "danger",
    })) return;
    const pw = prompt("Type your current master password to confirm the reset:");
    if (pw === null) return;
    try {
      await api("/api/vault/reset", { method: "POST",
                                       body: JSON.stringify({ master_password: pw }) });
      toast("✓ Vault reset");
      await reloadAll();
    } catch (e) { toast("Reset failed: " + e.message, true); }
  }

  // ─────────────────────────────────────────────────────────────
  // Manager — load + render
  // ─────────────────────────────────────────────────────────────
  async function loadManager() {
    try {
      const [kindsResp, profiles] = await Promise.all([
        api("/api/vault/kinds"),
        api("/api/profiles").catch(() => []),
      ]);
      state.kinds = kindsResp.kinds || {};
      state.profiles = profiles || [];
      renderKindFilter();
      await loadItems();
    } catch (e) {
      console.error("load manager:", e);
      toast("Failed to load vault: " + e.message, true);
    }
  }

  async function loadItems() {
    try {
      const qs = new URLSearchParams();
      if (state.filterKind)   qs.set("kind", state.filterKind);
      if (state.filterSearch) qs.set("q", state.filterSearch);
      const resp = await api(`/api/vault/items?${qs.toString()}`);
      state.items   = resp.items || [];
      state.summary = { by_kind: resp.by_kind || {},
                        by_status: resp.by_status || {} };
      renderKPIs();
      renderTable();
    } catch (e) { console.error("load items:", e); }
  }

  function renderKindFilter() {
    const host = $("#vault-kind-filter");
    // Keep the "All" button, append one button per kind
    const kinds = Object.entries(state.kinds);
    host.innerHTML =
      `<button class="cmp-period-btn active" data-kind="">All</button>` +
      kinds.map(([k, v]) => `
        <button class="cmp-period-btn" data-kind="${escapeHtml(k)}"
                title="${escapeHtml(v.tip || "")}">
          ${v.icon || ""} ${escapeHtml(v.label.split(" ")[0])}
        </button>
      `).join("");
  }

  function renderKPIs() {
    const bk = state.summary.by_kind   || {};
    const bs = state.summary.by_status || {};
    const total = Object.values(bk).reduce((a,b) => a + b, 0);
    $("#vault-kpi-total").textContent    = total;
    $("#vault-kpi-accounts").textContent = (bk.account || 0) + (bk.social || 0) + (bk.email || 0);
    $("#vault-kpi-wallets").textContent  = bk.crypto_wallet || 0;
    $("#vault-kpi-apikeys").textContent  = bk.api_key || 0;
    $("#vault-kpi-blocked").textContent  = (bs.banned || 0) + (bs.locked || 0);
  }

  function renderTable() {
    const tbody = $("#vault-tbody");
    if (!state.items.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="dense-empty-cell">
        ${state.filterSearch ? `No items match "${escapeHtml(state.filterSearch)}"` :
         state.filterKind    ? `No items of this kind yet` :
                               `No items yet — click <strong>+ Add item</strong> to store your first secret`}
      </td></tr>`;
      return;
    }
    tbody.innerHTML = state.items.map(it => {
      const kindInfo = state.kinds[it.kind] || {};
      const kindChip = `
        <span class="vault-kind-chip" title="${escapeHtml(kindInfo.label || it.kind)}">
          ${kindInfo.icon || "•"} ${escapeHtml(it.kind)}
        </span>`;
      const statusBadge = `
        <span class="vault-status vault-status-${escapeHtml(it.status)}">
          ${escapeHtml(it.status)}
        </span>`;
      const tagsHtml = (it.tags || []).slice(0, 3).map(t =>
        `<span class="sched-profile-tag">${escapeHtml(t)}</span>`).join(" ");
      return `
        <tr data-item="${it.id}">
          <td style="text-align:center;">${kindInfo.icon || "•"}</td>
          <td>${kindChip}</td>
          <td><strong>${escapeHtml(it.name)}</strong></td>
          <td class="muted" style="font-family: ui-monospace, monospace; font-size: 11.5px;">
            ${escapeHtml(it.identifier || "")}
          </td>
          <td class="muted">${escapeHtml(it.service || "—")}</td>
          <td>${tagsHtml || '<span class="muted">—</span>'}</td>
          <td class="muted">${escapeHtml(it.profile_name || "—")}</td>
          <td>${statusBadge}</td>
          <td class="vault-actions-cell">
            <button class="vault-action-btn" data-action="reveal"
                    title="Reveal decrypted secrets">👁</button>
            <button class="vault-action-btn" data-action="copy-totp"
                    ${it.has_secrets ? "" : "disabled"}
                    title="Copy current TOTP code">🔐</button>
            <button class="vault-action-btn" data-action="edit"
                    title="Edit">✎</button>
            <button class="vault-action-btn vault-action-del" data-action="delete"
                    title="Delete">✕</button>
          </td>
        </tr>`;
    }).join("");

    tbody.querySelectorAll("tr[data-item]").forEach(row => {
      const id = parseInt(row.dataset.item, 10);
      row.querySelectorAll("[data-action]").forEach(b => {
        b.addEventListener("click", () => handleAction(id, b.dataset.action));
      });
    });
  }

  async function handleAction(id, action) {
    if (action === "delete") {
      if (!await confirmDialog({
        title: "Delete item?",
        message: "This permanently removes the vault entry.",
        confirmText: "Delete", confirmStyle: "danger",
      })) return;
      try {
        await api(`/api/vault/items/${id}`, { method: "DELETE" });
        toast("✓ Deleted");
        loadItems();
      } catch (e) { toast("Delete failed: " + e.message, true); }
    } else if (action === "edit") {
      openEditor(id);
    } else if (action === "reveal") {
      openReveal(id);
    } else if (action === "copy-totp") {
      try {
        const r = await api(`/api/vault/items/${id}/totp`);
        await navigator.clipboard.writeText(r.code);
        toast(`✓ TOTP ${r.code} copied (rolls in ${r.remaining}s)`);
      } catch (e) { toast("TOTP unavailable: " + e.message, true); }
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Add / Edit modal
  // ─────────────────────────────────────────────────────────────
  async function openEditor(id) {
    state.editing = id;
    state.customFields = [];
    const title = $("#vault-edit-title");
    const kindWrap = $("#vault-edit-kind-wrap");

    renderKindGrid();
    populateProfileSelect();

    if (id) {
      title.textContent = "Edit vault item";
      kindWrap.style.display = "none";
      try {
        const item = await api(`/api/vault/items/${id}`);
        state.editKind = item.kind || "account";
        renderKindGrid();   // ensure the selected radio reflects the item
        $("#vault-f-name").value        = item.name || "";
        $("#vault-f-service").value     = item.service || "";
        $("#vault-f-identifier").value  = item.identifier || "";
        $("#vault-f-profile").value     = item.profile_name || "";
        $("#vault-f-status").value      = item.status || "active";
        $("#vault-f-tags").value        = (item.tags || []).join(", ");
        $("#vault-f-notes").value       = item.notes || "";
        renderSecretFields(item.secrets || {});
      } catch (e) {
        toast("Load failed: " + e.message, true);
        return;
      }
    } else {
      title.textContent = "Add vault item";
      kindWrap.style.display = "block";
      state.editKind = "account";
      renderKindGrid();
      $("#vault-f-name").value        = "";
      $("#vault-f-service").value     = "";
      $("#vault-f-identifier").value  = "";
      $("#vault-f-profile").value     = "";
      $("#vault-f-status").value      = "active";
      $("#vault-f-tags").value        = "";
      $("#vault-f-notes").value       = "";
      renderSecretFields({});
    }

    $("#vault-edit-modal").style.display = "flex";
    setTimeout(() => $("#vault-f-name").focus(), 50);
  }

  function renderKindGrid() {
    const host = $("#vault-kind-grid");
    host.innerHTML = Object.entries(state.kinds).map(([k, v]) => `
      <label class="vault-kind-card ${state.editKind === k ? "selected" : ""}">
        <input type="radio" name="vault-kind" value="${k}"
               ${state.editKind === k ? "checked" : ""}>
        <div class="vault-kind-card-body">
          <div class="vault-kind-card-title">${v.icon || ""} ${escapeHtml(v.label)}</div>
          <div class="vault-kind-card-tip">${escapeHtml(v.tip || "")}</div>
        </div>
      </label>
    `).join("");
    host.querySelectorAll('input[name="vault-kind"]').forEach(r => {
      r.addEventListener("change", () => {
        state.editKind = r.value;
        renderKindGrid();
        renderSecretFields({});
      });
    });
  }

  function renderSecretFields(presetValues) {
    const host = $("#vault-f-secrets");
    const info = state.kinds[state.editKind] || {};
    const identInfo = info.identifier || { label: "Identifier", placeholder: "" };
    $("#vault-f-ident-label").textContent = identInfo.label;
    $("#vault-f-identifier").placeholder  = identInfo.placeholder || "";
    $("#vault-f-ident-hint").textContent  = "";

    if (state.editKind === "custom") {
      host.innerHTML = "";
      $("#vault-f-custom-editor").style.display = "block";
      state.customFields = Object.entries(presetValues || {}).map(([k, v]) =>
        ({ key: k, value: v }));
      if (!state.customFields.length) state.customFields = [{ key: "", value: "" }];
      renderCustomFields();
      return;
    }
    $("#vault-f-custom-editor").style.display = "none";

    const fields = info.secret_fields || [];
    host.innerHTML = fields.map(f => {
      const val = presetValues[f.key] || "";
      const inp = f.kind === "multiline"
        ? `<textarea class="input dense-textarea" rows="3"
                    data-secret="${escapeHtml(f.key)}"
                    placeholder="${escapeHtml(f.placeholder || "")}">${escapeHtml(val)}</textarea>`
        : `<input type="password" class="input" data-secret="${escapeHtml(f.key)}"
                  value="${escapeHtml(val)}"
                  placeholder="${escapeHtml(f.placeholder || "")}"
                  autocomplete="new-password">`;
      return `
        <div class="form-group">
          <label class="form-label">${escapeHtml(f.label)}</label>
          ${inp}
        </div>`;
    }).join("");
  }

  function renderCustomFields() {
    const host = $("#vault-f-custom-list");
    host.innerHTML = state.customFields.map((row, idx) => `
      <div class="vault-custom-row" data-idx="${idx}">
        <input type="text" class="input vault-custom-key" placeholder="field name"
               value="${escapeHtml(row.key)}">
        <input type="text" class="input vault-custom-val" placeholder="value"
               value="${escapeHtml(row.value)}">
        <button class="btn btn-secondary btn-small btn-danger vault-custom-del"
                data-idx="${idx}">✕</button>
      </div>
    `).join("");
    host.querySelectorAll(".vault-custom-key").forEach(inp =>
      inp.addEventListener("input", (e) => {
        state.customFields[parseInt(e.target.closest("[data-idx]").dataset.idx, 10)].key = e.target.value;
      }));
    host.querySelectorAll(".vault-custom-val").forEach(inp =>
      inp.addEventListener("input", (e) => {
        state.customFields[parseInt(e.target.closest("[data-idx]").dataset.idx, 10)].value = e.target.value;
      }));
    host.querySelectorAll(".vault-custom-del").forEach(b =>
      b.addEventListener("click", () => {
        state.customFields.splice(parseInt(b.dataset.idx, 10), 1);
        renderCustomFields();
      }));
  }

  function populateProfileSelect() {
    const sel = $("#vault-f-profile");
    sel.innerHTML = `<option value="">— none —</option>` +
      (state.profiles || []).map(p => `
        <option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join("");
  }

  async function saveItem() {
    const name = $("#vault-f-name").value.trim();
    if (!name) { toast("Name is required", true); return; }

    let secrets = {};
    if (state.editKind === "custom") {
      for (const f of state.customFields) {
        if (f.key.trim()) secrets[f.key.trim()] = f.value;
      }
    } else {
      $$("#vault-f-secrets [data-secret]").forEach(el => {
        if ((el.value || "").length) secrets[el.dataset.secret] = el.value;
      });
    }
    if (!Object.keys(secrets).length) secrets = null;

    const tagsRaw = $("#vault-f-tags").value;
    const tags = tagsRaw ? tagsRaw.split(/[,\s]+/).filter(Boolean) : [];

    const body = {
      name,
      kind:         state.editKind,
      service:      $("#vault-f-service").value.trim() || null,
      identifier:   $("#vault-f-identifier").value.trim() || null,
      secrets,
      profile_name: $("#vault-f-profile").value || null,
      status:       $("#vault-f-status").value,
      tags,
      notes:        $("#vault-f-notes").value.trim() || null,
    };

    try {
      if (state.editing) {
        await api(`/api/vault/items/${state.editing}`,
                  { method: "PUT", body: JSON.stringify(body) });
        toast("✓ Updated");
      } else {
        await api("/api/vault/items",
                  { method: "POST", body: JSON.stringify(body) });
        toast("✓ Added");
      }
      $("#vault-edit-modal").style.display = "none";
      await loadItems();
    } catch (e) { toast("Save failed: " + e.message, true); }
  }

  // ─────────────────────────────────────────────────────────────
  // Reveal (decrypted view)
  // ─────────────────────────────────────────────────────────────
  async function openReveal(id) {
    const body = $("#vault-reveal-body");
    body.innerHTML = '<div class="muted">Decrypting…</div>';
    $("#vault-reveal-modal").style.display = "flex";
    try {
      const item = await api(`/api/vault/items/${id}`);
      $("#vault-reveal-title").textContent = `🔓 ${item.name}`;
      const secrets = item.secrets || {};
      const rows = Object.entries(secrets).map(([k, v]) => `
        <div class="vault-reveal-row">
          <div class="vault-reveal-key">${escapeHtml(k)}</div>
          <pre class="vault-reveal-val">${escapeHtml(String(v))}</pre>
          <button class="btn btn-secondary btn-small" data-copy="${escapeHtml(String(v))}">Copy</button>
        </div>`).join("");
      body.innerHTML = `
        <div class="vault-reveal-meta muted">
          Kind: ${escapeHtml(item.kind)} · Service: ${escapeHtml(item.service || "—")} ·
          Identifier: ${escapeHtml(item.identifier || "—")}
        </div>
        ${rows || '<div class="muted">No secrets stored for this item.</div>'}
      `;
      body.querySelectorAll("[data-copy]").forEach(b =>
        b.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(b.dataset.copy);
            toast("✓ Copied");
          } catch (e) { toast("Copy failed", true); }
        }));
    } catch (e) {
      body.innerHTML = `<div class="dense-callout" style="color:#fca5a5;">
        ${escapeHtml(e.message)}</div>`;
    }
  }

  return { init };
})();
