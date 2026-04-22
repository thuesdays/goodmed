// ═══════════════════════════════════════════════════════════════
// pages/actions.js — visual pipeline builder for post-ad actions
//
// Two pipelines:
//   post_ad_actions          — runs for every competitor ad
//   on_target_domain_actions — runs when ad's domain is your own
// ═══════════════════════════════════════════════════════════════

const ActionsPage = {
  catalog: [],               // list of available action types + metadata
  commonParams: [],          // params that apply to every action type
  pipelines: {               // current state of both pipelines
    post_ad_actions: [],
    on_target_domain_actions: [],
  },
  dirty: false,              // unsaved changes
  addTarget: null,           // which pipeline the add-step modal is for

  async init() {
    await Promise.all([this.loadCatalog(), this.loadPipelines()]);

    // Re-load button
    $("#actions-reload-btn").addEventListener("click", () => this.init());
    // Save button
    $("#actions-save-btn").addEventListener("click", () => this.save());

    // Add-step buttons (both pipelines)
    document.querySelectorAll('[data-action="add"]').forEach(btn => {
      btn.addEventListener("click", () => this.openAddModal(btn.dataset.pipeline));
    });

    // Modal wiring
    document.querySelectorAll("[data-close]").forEach(el =>
      el.addEventListener("click", () => this.closeModal(el.dataset.close))
    );
    $("#add-step-type").addEventListener("change",
      () => this.renderTypeParams());
    $("#add-step-confirm-btn").addEventListener("click",
      () => this.confirmAddStep());

    // Warn on unsaved changes
    window.addEventListener("beforeunload", e => {
      if (this.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  },

  // ── Loading ──────────────────────────────────────────────────

  async loadCatalog() {
    try {
      const resp = await api("/api/actions/catalog");
      // Backend returns {types: [...], common_params: [...]}
      // Older format was just [...] — support both gracefully
      if (Array.isArray(resp)) {
        this.catalog = resp;
        this.commonParams = [];
      } else {
        this.catalog      = resp.types || [];
        this.commonParams = resp.common_params || [];
      }
      this.renderCatalog();
      this.populateTypeSelect();
    } catch (e) {
      console.error("catalog load:", e);
      toast("Failed to load action catalog", true);
    }
  },

  async loadPipelines() {
    try {
      const p = await api("/api/actions/pipelines");
      this.pipelines.post_ad_actions          = p.post_ad_actions || [];
      this.pipelines.on_target_domain_actions = p.on_target_domain_actions || [];
      this.dirty = false;
      this.renderPipelines();
    } catch (e) {
      console.error("pipelines load:", e);
    }
  },

  // ── Rendering ────────────────────────────────────────────────

  renderCatalog() {
    const el = $("#actions-catalog");
    $("#catalog-count").textContent = this.catalog.length;
    if (!this.catalog.length) {
      el.innerHTML = `<div class="empty-state">No actions available</div>`;
      return;
    }
    el.innerHTML = this.catalog.map(a => `
      <div class="catalog-card" data-type="${escapeHtml(a.type)}">
        <div class="catalog-card-label">${escapeHtml(a.label)}</div>
        <div class="catalog-card-type">${escapeHtml(a.type)}</div>
        <div class="catalog-card-desc">${escapeHtml(a.description || "")}</div>
      </div>
    `).join("");
    // Click-to-add
    el.querySelectorAll(".catalog-card").forEach(card =>
      card.addEventListener("click", () => {
        // Default to "post_ad_actions" when clicked from catalog
        this.openAddModal("post_ad_actions", card.dataset.type);
      })
    );
  },

  renderPipelines() {
    this.renderOnePipeline("post_ad_actions",
                           "#post-ad-list", "#post-ad-count");
    this.renderOnePipeline("on_target_domain_actions",
                           "#target-action-list", "#target-action-count");
  },

  renderOnePipeline(key, listSel, badgeSel) {
    const list  = $(listSel);
    const badge = $(badgeSel);
    const pipeline = this.pipelines[key] || [];
    badge.textContent = pipeline.length;

    if (!pipeline.length) {
      list.innerHTML = `
        <div class="pipeline-empty">
          No steps yet. Click <strong>+ Add step</strong> above to start.
        </div>`;
      return;
    }

    list.innerHTML = pipeline.map((step, i) =>
      this.renderStep(key, step, i)).join("");

    // Wire up buttons on each row
    list.querySelectorAll(".pipeline-step").forEach(row => {
      const idx  = parseInt(row.dataset.index, 10);
      row.querySelector(".step-up-btn")?.addEventListener(
        "click", () => this.moveStep(key, idx, -1));
      row.querySelector(".step-down-btn")?.addEventListener(
        "click", () => this.moveStep(key, idx, +1));
      row.querySelector(".step-remove-btn")?.addEventListener(
        "click", () => this.removeStep(key, idx));
      row.querySelector(".step-enabled-toggle")?.addEventListener(
        "change", e => this.toggleStep(key, idx, e.target.checked));
      // Inline params editing
      row.querySelectorAll("[data-param]").forEach(input => {
        input.addEventListener("change",
          () => this.updateStepParam(key, idx, input));
      });
    });
  },

  renderStep(pipelineKey, step, idx) {
    const meta = this.catalog.find(c => c.type === step.type);
    const label = meta ? meta.label : step.type;
    const enabled = step.enabled !== false;
    const prob = step.probability !== undefined
                  ? Number(step.probability) : 1.0;

    // Build the "summary" of parameters (non-default values only)
    const summary = this.buildStepSummary(step, meta);

    // Extra badges for common-params flags
    const badges = [];
    if (prob < 1.0) {
      badges.push(`<span class="step-prob-badge">p=${prob.toFixed(2)}</span>`);
    }
    if (step.skip_on_my_domain) {
      badges.push(`<span class="step-skip-badge" title="Skipped when ad is on my domain">skip my</span>`);
    }
    if (step.skip_on_target) {
      badges.push(`<span class="step-skip-badge" title="Skipped when ad is on target domain">skip target</span>`);
    }

    return `
      <div class="pipeline-step ${enabled ? "" : "disabled"}"
           data-index="${idx}" data-pipeline="${pipelineKey}">
        <div class="step-main">
          <div class="step-number">${idx + 1}</div>

          <div class="step-info">
            <div class="step-label">
              <label class="step-enabled-check">
                <input type="checkbox" class="step-enabled-toggle"
                       ${enabled ? "checked" : ""}>
              </label>
              <strong>${escapeHtml(label)}</strong>
              <code class="step-type-tag">${escapeHtml(step.type)}</code>
              ${badges.join("")}
            </div>
            <div class="step-summary">${summary}</div>
          </div>

          <div class="step-actions">
            <button class="btn-icon step-up-btn"   title="Move up">↑</button>
            <button class="btn-icon step-down-btn" title="Move down">↓</button>
            <button class="btn-icon step-remove-btn danger" title="Delete">×</button>
          </div>
        </div>

        <div class="step-params">
          ${this.renderStepParams(step, meta)}
        </div>
      </div>
    `;
  },

  buildStepSummary(step, meta) {
    // Show first 2-3 non-default params as text summary
    if (!meta || !meta.params?.length) return "";
    const parts = [];
    for (const p of meta.params) {
      const val = step[p.name];
      if (val === undefined || val === null || val === "") continue;
      if (val === p.default) continue;
      if (p.type === "bool") {
        parts.push(`${p.label || p.name}: ${val ? "yes" : "no"}`);
      } else {
        parts.push(`${p.label || p.name}: <code>${escapeHtml(String(val))}</code>`);
      }
      if (parts.length >= 3) break;
    }
    return parts.length ? parts.join(" · ")
                        : '<span class="muted">using defaults</span>';
  },

  renderStepParams(step, meta) {
    const inputs = [];
    // Type-specific params first
    if (meta?.params?.length) {
      for (const p of meta.params) {
        const val = step[p.name] !== undefined ? step[p.name] : p.default;
        inputs.push(this.renderParamInput(p, val));
      }
    }
    // Common params (probability, skip flags)
    for (const p of this.commonParams) {
      const val = step[p.name] !== undefined ? step[p.name] : p.default;
      inputs.push(this.renderParamInput(p, val));
    }

    if (!inputs.length) {
      return `<div class="step-no-params">No parameters</div>`;
    }
    return inputs.join("");
  },

  renderParamInput(p, val) {
    const label = escapeHtml(p.label || p.name);
    const name  = escapeHtml(p.name);
    const ph    = p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : "";

    if (p.type === "bool") {
      return `
        <div class="param-row">
          <label>${label}</label>
          <input type="checkbox" data-param="${name}"
                 ${val ? "checked" : ""}>
        </div>`;
    }
    if (p.type === "number") {
      return `
        <div class="param-row">
          <label>${label}</label>
          <input type="number" data-param="${name}" step="0.1"
                 value="${val !== undefined && val !== null ? val : ""}">
        </div>`;
    }
    if (p.type === "select") {
      const opts = (p.options || []).map(o =>
        `<option value="${escapeHtml(o)}" ${o === val ? "selected" : ""}>${escapeHtml(o)}</option>`
      ).join("");
      return `
        <div class="param-row">
          <label>${label}</label>
          <select data-param="${name}">${opts}</select>
        </div>`;
    }
    // text default
    return `
      <div class="param-row">
        <label>${label}</label>
        <input type="text" data-param="${name}" ${ph}
               value="${val !== undefined && val !== null ? escapeHtml(String(val)) : ""}">
      </div>`;
  },

  // ── Step manipulation ────────────────────────────────────────

  moveStep(key, idx, delta) {
    const arr = this.pipelines[key];
    const tgt = idx + delta;
    if (tgt < 0 || tgt >= arr.length) return;
    [arr[idx], arr[tgt]] = [arr[tgt], arr[idx]];
    this.dirty = true;
    this.renderPipelines();
  },

  removeStep(key, idx) {
    this.pipelines[key].splice(idx, 1);
    this.dirty = true;
    this.renderPipelines();
  },

  toggleStep(key, idx, enabled) {
    this.pipelines[key][idx].enabled = enabled;
    this.dirty = true;
    // Live toggle of .disabled class without re-render to keep focus
    const row = document.querySelector(
      `.pipeline-step[data-pipeline="${key}"][data-index="${idx}"]`);
    if (row) row.classList.toggle("disabled", !enabled);
  },

  updateStepParam(key, idx, input) {
    const step  = this.pipelines[key][idx];
    const name  = input.dataset.param;
    let value;
    if (input.type === "checkbox")       value = input.checked;
    else if (input.type === "number")    value = input.value === ""
                                                   ? null
                                                   : parseFloat(input.value);
    else                                 value = input.value;
    step[name] = value;
    this.dirty = true;
    // Update summary text only (not full re-render)
    const row = document.querySelector(
      `.pipeline-step[data-pipeline="${key}"][data-index="${idx}"]`);
    if (row) {
      const meta = this.catalog.find(c => c.type === step.type);
      row.querySelector(".step-summary").innerHTML =
        this.buildStepSummary(step, meta);
      const probBadge = row.querySelector(".step-prob-badge");
      const p = step.probability !== undefined ? step.probability : 1.0;
      if (p < 1.0) {
        if (probBadge) {
          probBadge.textContent = `p=${Number(p).toFixed(2)}`;
        } else {
          const label = row.querySelector(".step-label");
          const badge = document.createElement("span");
          badge.className = "step-prob-badge";
          badge.textContent = `p=${Number(p).toFixed(2)}`;
          label.appendChild(badge);
        }
      } else if (probBadge) {
        probBadge.remove();
      }
    }
  },

  // ── Add-step modal ───────────────────────────────────────────

  populateTypeSelect() {
    const sel = $("#add-step-type");
    sel.innerHTML = this.catalog.map(a =>
      `<option value="${escapeHtml(a.type)}">${escapeHtml(a.label)}</option>`
    ).join("");
  },

  openAddModal(pipelineKey, preselectType = null) {
    this.addTarget = pipelineKey;
    if (preselectType) $("#add-step-type").value = preselectType;
    this.renderTypeParams();
    $("#add-step-modal").style.display = "flex";
  },

  closeModal(id) {
    $("#" + id).style.display = "none";
  },

  renderTypeParams() {
    const type = $("#add-step-type").value;
    const meta = this.catalog.find(c => c.type === type);
    $("#add-step-description").textContent = meta?.description || "";
    const container = $("#add-step-params");

    // Type-specific params
    let html = "";
    if (!meta || !meta.params?.length) {
      html += `<div class="form-hint">No type-specific parameters.</div>`;
    } else {
      html += meta.params.map(p => this._renderModalParam(p)).join("");
    }

    // Common params (probability, skip flags) — shared across all types
    if (this.commonParams.length) {
      html += `
        <div class="modal-section-label">Common options</div>
      `;
      html += this.commonParams
        .map(p => this._renderModalParam(p, true))
        .join("");
    }

    container.innerHTML = html;
  },

  _renderModalParam(p, isCommon = false) {
    const label = escapeHtml(p.label || p.name);
    const name  = escapeHtml(p.name);
    const ph    = p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : "";
    const hint  = p.hint ? `<div class="form-hint">${escapeHtml(p.hint)}</div>` : "";
    const scope = isCommon ? "data-common" : "data-addparam";

    if (p.type === "bool") {
      return `
        <div class="form-group">
          <label class="checkbox-label-inline">
            <input type="checkbox" ${scope}="${name}"
                   ${p.default ? "checked" : ""}>
            ${label}
          </label>
          ${hint}
        </div>`;
    }
    if (p.type === "number") {
      const step = p.step !== undefined ? p.step : 0.1;
      const min  = p.min  !== undefined ? `min="${p.min}"` : "";
      const max  = p.max  !== undefined ? `max="${p.max}"` : "";
      return `
        <div class="form-group">
          <label class="form-label">${label}${p.required ? " *" : ""}</label>
          <input type="number" class="input" step="${step}" ${min} ${max}
                 ${scope}="${name}"
                 value="${p.default !== undefined ? p.default : ""}">
          ${hint}
        </div>`;
    }
    if (p.type === "select") {
      const opts = (p.options || []).map(o =>
        `<option value="${escapeHtml(o)}" ${o === p.default ? "selected" : ""}>${escapeHtml(o)}</option>`
      ).join("");
      return `
        <div class="form-group">
          <label class="form-label">${label}${p.required ? " *" : ""}</label>
          <select class="select" ${scope}="${name}">${opts}</select>
          ${hint}
        </div>`;
    }
    return `
      <div class="form-group">
        <label class="form-label">${label}${p.required ? " *" : ""}</label>
        <input type="text" class="input" ${scope}="${name}" ${ph}
               value="${p.default !== undefined ? escapeHtml(String(p.default)) : ""}">
        ${hint}
      </div>`;
  },

  confirmAddStep() {
    const type = $("#add-step-type").value;
    const meta = this.catalog.find(c => c.type === type);
    if (!meta) { toast("Select a type", true); return; }

    const step = { type, enabled: true };

    // Type-specific params
    try {
      document.querySelectorAll("[data-addparam]").forEach(inp => {
        const name = inp.dataset.addparam;
        let val;
        if (inp.type === "checkbox")     val = inp.checked;
        else if (inp.type === "number")  val = inp.value === ""
                                                 ? null
                                                 : parseFloat(inp.value);
        else                             val = inp.value;
        // Validate required
        const pmeta = meta.params?.find(p => p.name === name);
        if (pmeta?.required && (val === "" || val === null)) {
          throw new Error(`Field "${pmeta.label || pmeta.name}" is required`);
        }
        if (val !== null && val !== "") step[name] = val;
      });
    } catch (e) {
      toast(e.message, true);
      return;
    }

    // Common params — only write if they differ from defaults, to keep
    // stored JSON minimal
    document.querySelectorAll("[data-common]").forEach(inp => {
      const name = inp.dataset.common;
      const pmeta = this.commonParams.find(p => p.name === name);
      if (!pmeta) return;
      let val;
      if (inp.type === "checkbox")     val = inp.checked;
      else if (inp.type === "number")  val = inp.value === ""
                                               ? null
                                               : parseFloat(inp.value);
      else                             val = inp.value;
      if (val !== pmeta.default && val !== null && val !== "") {
        step[name] = val;
      }
    });

    this.pipelines[this.addTarget].push(step);
    this.dirty = true;
    this.closeModal("add-step-modal");
    this.renderPipelines();
    toast(`✓ Added "${meta.label}" to pipeline`);
  },

  // ── Save ─────────────────────────────────────────────────────

  async save() {
    const btn = $("#actions-save-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Saving…";
    try {
      const r = await api("/api/actions/pipelines", {
        method: "POST",
        body: JSON.stringify(this.pipelines),
      });
      if (r.ok) {
        this.dirty = false;
        toast(
          `✓ Saved — default: ${r.saved.post_ad_actions ?? 0} step(s), ` +
          `target: ${r.saved.on_target_domain_actions ?? 0} step(s)`
        );
      } else {
        toast(r.error || "Save failed", true);
      }
    } catch (e) {
      toast(e.message || "Save failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "💾 Save changes";
    }
  },
};
