// ═══════════════════════════════════════════════════════════════
// pages/scripts.js — visual script builder
//
// Two pipelines:
//   main_script              — top-level loop (search queries, rotate, pause)
//                              only `scope: "loop"` actions belong here
//   post_ad_actions          — per-ad pipeline (runs once per ad found)
//                              step-level flags (only_on_target / skip_on_target
//                              / skip_on_my_domain) handle target vs competitor
//                              differentiation — no separate pipeline needed
//
// Legacy on_target_domain_actions is auto-migrated on load into
// post_ad_actions with `only_on_target: true` on each step, so old
// configs keep working.
// ═══════════════════════════════════════════════════════════════

const ScriptsPage = {
  catalog: [],               // list of available action types + metadata
  commonParams: [],          // params that apply to every per-ad action
  pipelines: {
    main_script:     [],
    post_ad_actions: [],
  },
  // Which scope each pipeline accepts
  pipelineScopes: {
    main_script:     "loop",
    post_ad_actions: "per_ad",
  },
  dirty: false,
  addTarget: null,

  async init() {
    // Catalog MUST finish before loadPipelines runs renderPipelines() —
    // the step renderer looks up each step's type in this.catalog to
    // resolve labels and params. Previously we ran both in parallel and
    // if pipelines resolved first the UI showed "UNKNOWN TYPE" for every
    // step until the user pressed Reload.
    await this.loadCatalog();
    await this.loadPipelines();

    $("#actions-reload-btn").addEventListener("click", () => this.init());
    $("#actions-save-btn").addEventListener("click", () => this.save());

    // Add-step buttons (three pipelines)
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

    window.addEventListener("beforeunload", e => {
      if (this.dirty) { e.preventDefault(); e.returnValue = ""; }
    });
  },

  // ── Loading ──────────────────────────────────────────────────

  async loadCatalog() {
    try {
      const resp = await api("/api/actions/catalog");
      if (Array.isArray(resp)) {
        this.catalog = resp;
        this.commonParams = [];
      } else {
        this.catalog      = resp.types || [];
        this.commonParams = resp.common_params || [];
      }
      // Default scope for older entries that didn't declare one
      this.catalog.forEach(c => { if (!c.scope) c.scope = "per_ad"; });
      this.renderCatalog();
    } catch (e) {
      console.error("catalog load:", e);
      toast("Failed to load action catalog", true);
    }
  },

  async loadPipelines() {
    try {
      const p = await api("/api/actions/pipelines");
      this.pipelines.main_script     = p.main_script     || [];
      this.pipelines.post_ad_actions = p.post_ad_actions || [];

      // Legacy migration: if the old on_target_domain_actions pipeline
      // is still populated in the DB, auto-merge its steps into
      // post_ad_actions with `only_on_target: true` so they only fire
      // for target-domain ads. One-time transparent migration — user
      // sees a merged list + a toast, save persists the new shape.
      const legacy = p.on_target_domain_actions || [];
      if (legacy.length) {
        const migrated = legacy.map(step => ({
          ...step,
          only_on_target: true,
        }));
        this.pipelines.post_ad_actions =
          this.pipelines.post_ad_actions.concat(migrated);
        this.dirty = true;
        toast(
          `Merged ${legacy.length} legacy target-domain step${legacy.length === 1 ? "" : "s"} ` +
          `into Per-ad actions. Click Save to persist.`
        );
      }

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
    // Group by scope
    const groups = {
      loop:   this.catalog.filter(c => c.scope === "loop"),
      per_ad: this.catalog.filter(c => c.scope !== "loop"),
    };

    const renderCard = (a) => `
      <div class="catalog-card" data-type="${escapeHtml(a.type)}"
           data-scope="${escapeHtml(a.scope)}">
        <div class="catalog-card-header">
          <span class="catalog-card-label">${escapeHtml(a.label)}</span>
          <span class="scope-badge scope-${a.scope === 'loop' ? 'loop' : 'per-ad'}">${
            a.scope === "loop" ? "loop" : "per ad"
          }</span>
        </div>
        <div class="catalog-card-type">${escapeHtml(a.type)}</div>
        <div class="catalog-card-desc">${escapeHtml(a.description || "")}</div>
      </div>`;

    el.innerHTML = `
      <div class="catalog-group-label">Script-level (main script)</div>
      <div class="catalog-grid">${groups.loop.map(renderCard).join("")}</div>
      <div class="catalog-group-label" style="margin-top: 18px;">Per-ad (inside a search)</div>
      <div class="catalog-grid">${groups.per_ad.map(renderCard).join("")}</div>
    `;

    // Click-to-add — auto-pick target pipeline by scope
    el.querySelectorAll(".catalog-card").forEach(card =>
      card.addEventListener("click", () => {
        const scope = card.dataset.scope;
        const target = scope === "loop" ? "main_script" : "post_ad_actions";
        this.openAddModal(target, card.dataset.type);
      })
    );
  },

  renderPipelines() {
    this.renderOnePipeline("main_script",
                           "#main-script-list", "#main-script-count");
    this.renderOnePipeline("post_ad_actions",
                           "#post-ad-list", "#post-ad-count");
  },

  renderOnePipeline(key, listSel, badgeSel) {
    const list  = $(listSel);
    const badge = $(badgeSel);
    if (!list) return;  // Pipeline section might not be on page

    const pipeline = this.pipelines[key] || [];
    if (badge) badge.textContent = pipeline.length;

    if (!pipeline.length) {
      const scope = this.pipelineScopes[key];
      const hint  = scope === "loop"
        ? "Start with a <strong>+ Add step</strong> → <em>Run one search query</em> or <em>Run ALL queries</em>."
        : "Add a step like <em>click_ad</em> or <em>visit</em> to react to matching ads.";
      list.innerHTML = `
        <div class="pipeline-empty">
          No steps yet. ${hint}
        </div>`;
      return;
    }

    list.innerHTML = pipeline.map(
      (step, i) => this.renderStep(key, step, i)
    ).join("");

    // Wire per-step controls
    list.querySelectorAll(".pipeline-step").forEach(row => {
      const idx = Number(row.dataset.index);
      const k   = row.dataset.pipeline;

      row.querySelector(".step-up-btn")?.addEventListener("click",
        () => this.moveStep(k, idx, -1));
      row.querySelector(".step-down-btn")?.addEventListener("click",
        () => this.moveStep(k, idx, +1));
      row.querySelector(".step-remove-btn")?.addEventListener("click",
        () => this.removeStep(k, idx));
      row.querySelector(".step-enabled-toggle")?.addEventListener("change",
        (e) => this.toggleStep(k, idx, e.target.checked));

      // Param inputs
      row.querySelectorAll("[data-param]").forEach(input => {
        input.addEventListener("change",
          () => this.updateStepParam(k, idx, input));
      });

      // ── Nested-steps controls (inside a loop action) ──
      row.querySelector(".nested-add-btn")?.addEventListener("click", (e) => {
        const parent = Number(e.currentTarget.dataset.parent);
        this.openAddModal(k, null, { parentIdx: parent });
      });
      row.querySelectorAll(".nested-step").forEach(nrow => {
        const parentIdx = Number(nrow.dataset.parent);
        const nIdx      = Number(nrow.dataset.nidx);

        nrow.querySelector(".nested-up-btn")?.addEventListener("click",
          () => this.moveNested(k, parentIdx, nIdx, -1));
        nrow.querySelector(".nested-down-btn")?.addEventListener("click",
          () => this.moveNested(k, parentIdx, nIdx, +1));
        nrow.querySelector(".nested-remove-btn")?.addEventListener("click",
          () => this.removeNested(k, parentIdx, nIdx));
      });
    });
  },

  renderStep(pipelineKey, step, idx) {
    const meta = this.catalog.find(c => c.type === step.type);
    const label = meta ? meta.label : step.type;
    const enabled = step.enabled !== false;
    const prob = step.probability !== undefined
                  ? Number(step.probability) : 1.0;
    const summary = this.buildStepSummary(step, meta);

    const badges = [];
    if (!meta) {
      badges.push(
        `<span class="step-skip-badge" title="This action type is not in the current catalog. Remove and re-add.">UNKNOWN TYPE</span>`
      );
    } else if (meta.scope === "loop") {
      badges.push(`<span class="scope-badge scope-loop">loop</span>`);
    } else {
      badges.push(`<span class="scope-badge scope-per-ad">per ad</span>`);
    }
    if (prob < 1.0) {
      badges.push(`<span class="step-prob-badge">p=${prob.toFixed(2)}</span>`);
    }
    if (step.skip_on_my_domain) {
      badges.push(`<span class="step-skip-badge" title="Skipped when ad is on my domain">skip my</span>`);
    }
    if (step.skip_on_target) {
      badges.push(`<span class="step-skip-badge" title="Skipped when ad is on target domain">skip target</span>`);
    }
    if (step.only_on_target) {
      badges.push(`<span class="step-only-badge" title="Runs ONLY for target-domain ads">only target</span>`);
    }
    if (step.only_on_my_domain) {
      badges.push(`<span class="step-only-badge" title="Runs ONLY for my-domain ads">only my</span>`);
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
            <button class="btn-icon step-remove-btn" title="Remove">✕</button>
          </div>
        </div>
        <div class="step-params">
          ${this.renderStepParams(step, meta, pipelineKey)}
        </div>
      </div>`;
  },

  buildStepSummary(step, meta) {
    if (!meta || !meta.params?.length) return "";
    const parts = [];
    for (const p of meta.params) {
      const val = step[p.name];
      if (val === undefined || val === null || val === "") continue;

      // Textlist — summarize as count + first item
      if (p.type === "textlist") {
        if (!Array.isArray(val) || !val.length) continue;
        const head = val.length > 1
          ? `${val[0]} · +${val.length - 1} more`
          : val[0];
        parts.push(`${p.label || p.name}: <code>${escapeHtml(head)}</code>`);
        if (parts.length >= 3) break;
        continue;
      }
      // Nested steps — summarize as count
      if (p.type === "steps") {
        if (Array.isArray(val) && val.length) {
          parts.push(`${val.length} nested step${val.length === 1 ? "" : "s"}`);
        }
        if (parts.length >= 3) break;
        continue;
      }

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

  renderStepParams(step, meta, pipelineKey) {
    // Two visual buckets:
    //   1. "inputs" — number / text / select / textlist fields. Grid layout.
    //   2. "flags"  — boolean checkboxes. Horizontal strip, one line.
    // Separating these stops the ugly mixing we had before (short number
    // input next to a 20-char label checkbox).
    const inputs   = [];
    const flags    = [];
    let nestedStepsUI = "";

    const pushField = (p, val) => {
      if (p.type === "steps") {
        nestedStepsUI = this.renderNestedStepsBlock(step, p, pipelineKey);
        return;
      }
      if (p.type === "bool") {
        flags.push(this.renderParamInput(p, val));
      } else {
        inputs.push(this.renderParamInput(p, val));
      }
    };

    if (meta?.params?.length) {
      for (const p of meta.params) {
        const val = step[p.name] !== undefined ? step[p.name] : p.default;
        pushField(p, val);
      }
    }
    // Common params (probability, skip flags) only apply to per-ad steps.
    if (pipelineKey !== "main_script") {
      for (const p of this.commonParams) {
        const val = step[p.name] !== undefined ? step[p.name] : p.default;
        pushField(p, val);
      }
    }

    if (!inputs.length && !flags.length && !nestedStepsUI) {
      return `<div class="step-no-params">No parameters</div>`;
    }

    let html = "";
    if (inputs.length) {
      html += `<div class="step-inputs-row">${inputs.join("")}</div>`;
    }
    if (flags.length) {
      html += `<div class="step-flags-row">${flags.join("")}</div>`;
    }
    if (nestedStepsUI) {
      html += nestedStepsUI;
    }
    return html;
  },

  /**
   * Render the "Nested steps" block shown inside a loop step's body.
   * Each nested step gets compact controls (type pill, short params
   * summary, move-up/down/remove). Add-step modal is re-used.
   */
  renderNestedStepsBlock(parentStep, paramMeta, parentPipeline) {
    const nested = Array.isArray(parentStep.steps) ? parentStep.steps : [];
    const label  = escapeHtml(paramMeta.label || "Nested steps");
    const hint   = paramMeta.hint
      ? `<div class="form-hint">${escapeHtml(paramMeta.hint)}</div>`
      : "";

    // Find the index of this parent step in its pipeline so the
    // nested-step handlers know where to write back.
    const parentIdx = this.pipelines[parentPipeline].indexOf(parentStep);

    const rows = nested.map((ns, i) => {
      const nMeta  = this.catalog.find(c => c.type === ns.type);
      const nLabel = nMeta ? nMeta.label : ns.type;
      const summary = this.buildStepSummary(ns, nMeta);
      return `
        <div class="nested-step" data-parent="${parentIdx}" data-nidx="${i}">
          <div class="nested-step-main">
            <span class="nested-step-num">${i + 1}</span>
            <strong>${escapeHtml(nLabel)}</strong>
            <code class="step-type-tag">${escapeHtml(ns.type)}</code>
            <span class="nested-step-summary">${summary}</span>
          </div>
          <div class="nested-step-actions">
            <button class="btn-icon nested-up-btn"     title="Move up">↑</button>
            <button class="btn-icon nested-down-btn"   title="Move down">↓</button>
            <button class="btn-icon nested-remove-btn" title="Remove">✕</button>
          </div>
        </div>`;
    }).join("");

    return `
      <div class="nested-steps-block">
        <div class="nested-steps-header">
          <span class="nested-steps-title">${label}
            <span class="badge">${nested.length}</span>
          </span>
          <button class="btn btn-secondary btn-small nested-add-btn"
                  data-parent="${parentIdx}">+ Add nested step</button>
        </div>
        ${hint}
        <div class="nested-steps-list">
          ${rows || '<div class="pipeline-empty" style="padding: 14px;">Empty — add steps that run for every item.</div>'}
        </div>
      </div>`;
  },

  renderParamInput(p, val) {
    const label = escapeHtml(p.label || p.name);
    const name  = escapeHtml(p.name);
    const ph    = p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : "";

    if (p.type === "bool") {
      return `
        <div class="param-row">
          <label class="checkbox-label-inline">
            <input type="checkbox" data-param="${name}"
                   ${val ? "checked" : ""}>
            ${label}
          </label>
        </div>`;
    }
    if (p.type === "number") {
      const step = p.step !== undefined ? p.step : 0.1;
      return `
        <div class="param-row">
          <label class="param-label">${label}</label>
          <input type="number" class="input param-input" step="${step}"
                 data-param="${name}" value="${val !== undefined && val !== null ? val : ""}">
        </div>`;
    }
    if (p.type === "select") {
      const opts = (p.options || []).map(o =>
        `<option value="${escapeHtml(o)}" ${o === val ? "selected" : ""}>${escapeHtml(o)}</option>`
      ).join("");
      return `
        <div class="param-row">
          <label class="param-label">${label}</label>
          <select class="select param-input" data-param="${name}">${opts}</select>
        </div>`;
    }
    if (p.type === "textlist") {
      // Edit as newline-separated string; stored as JSON array.
      const text = Array.isArray(val) ? val.join("\n") : (val || "");
      const rows = Math.max(3, Math.min(8, text.split("\n").length));
      return `
        <div class="param-row param-row-full">
          <label class="param-label">${label}</label>
          <textarea class="textarea param-input" rows="${rows}"
                    data-param="${name}" data-param-type="textlist"
                    ${ph}>${escapeHtml(text)}</textarea>
        </div>`;
    }
    return `
      <div class="param-row">
        <label class="param-label">${label}</label>
        <input type="text" class="input param-input" data-param="${name}"
               ${ph} value="${val !== undefined && val !== null ? escapeHtml(String(val)) : ""}">
      </div>`;
  },

  moveStep(key, idx, delta) {
    const p = this.pipelines[key];
    const j = idx + delta;
    if (j < 0 || j >= p.length) return;
    [p[idx], p[j]] = [p[j], p[idx]];
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
    this.renderPipelines();
  },

  // ── Nested-step mutations (inside a loop action's steps array) ──

  moveNested(key, parentIdx, nIdx, delta) {
    const parent = this.pipelines[key]?.[parentIdx];
    if (!parent?.steps) return;
    const j = nIdx + delta;
    if (j < 0 || j >= parent.steps.length) return;
    [parent.steps[nIdx], parent.steps[j]] = [parent.steps[j], parent.steps[nIdx]];
    this.dirty = true;
    this.renderPipelines();
  },

  removeNested(key, parentIdx, nIdx) {
    const parent = this.pipelines[key]?.[parentIdx];
    if (!parent?.steps) return;
    parent.steps.splice(nIdx, 1);
    this.dirty = true;
    this.renderPipelines();
  },

  addNested(key, parentIdx, newStep) {
    const parent = this.pipelines[key]?.[parentIdx];
    if (!parent) return;
    if (!Array.isArray(parent.steps)) parent.steps = [];
    parent.steps.push(newStep);
    this.dirty = true;
    this.renderPipelines();
  },

  updateStepParam(key, idx, input) {
    const step = this.pipelines[key][idx];
    const name = input.dataset.param;
    const ptype = input.dataset.paramType;   // set on textlist textarea

    let val;
    if (input.type === "checkbox") {
      val = input.checked;
    } else if (input.type === "number") {
      val = input.value === "" ? null : parseFloat(input.value);
    } else if (ptype === "textlist") {
      // Split newline-separated text into array, drop empty lines
      const lines = String(input.value || "")
        .split("\n")
        .map(s => s.trim())
        .filter(Boolean);
      val = lines.length ? lines : null;
    } else {
      val = input.value;
    }

    if (val === null || val === "" || (Array.isArray(val) && !val.length)) {
      delete step[name];
    } else {
      step[name] = val;
    }
    this.dirty = true;

    // Update badges without a full re-render — probability is the
    // only one that changes visibly as you type.
    const row = document.querySelector(
      `.pipeline-step[data-pipeline="${key}"][data-index="${idx}"]`
    );
    if (row && name === "probability") {
      const prob = step.probability !== undefined ? Number(step.probability) : 1.0;
      const probBadge = row.querySelector(".step-prob-badge");
      if (prob < 1.0) {
        if (probBadge) {
          probBadge.textContent = `p=${prob.toFixed(2)}`;
        } else {
          row.querySelector(".step-label").insertAdjacentHTML(
            "beforeend",
            `<span class="step-prob-badge">p=${prob.toFixed(2)}</span>`
          );
        }
      } else if (probBadge) {
        probBadge.remove();
      }
    }
  },

  // ── Add-step modal ───────────────────────────────────────────

  populateTypeSelect(pipelineKey, opts = {}) {
    const sel = $("#add-step-type");
    const isNested = !!opts.parentIdx;   // adding INTO a loop's steps[]
    // Scope rules:
    //   - main_script / nested-under-loop accept loop-scope actions only
    //   - per-ad pipelines accept per-ad actions only
    //   - loop-inside-loop is disallowed (avoid infinite recursion)
    const requiredScope = isNested
      ? "loop"
      : this.pipelineScopes[pipelineKey];

    const eligible = this.catalog.filter(a => {
      if (isNested && a.type === "loop") return false;   // no nested loops
      if (requiredScope === "loop")   return a.scope === "loop";
      if (requiredScope === "per_ad") return a.scope !== "loop";
      return true;
    });

    if (!eligible.length) {
      sel.innerHTML = `<option disabled>No compatible actions</option>`;
      return;
    }
    sel.innerHTML = eligible.map(a =>
      `<option value="${escapeHtml(a.type)}">${escapeHtml(a.label)}</option>`
    ).join("");
  },

  /**
   * Open the Add-step modal.
   *
   * @param {string}  pipelineKey     which top-level pipeline (main_script / post_ad_actions / ...)
   * @param {string?} preselectType   optional — default-selected type in the dropdown
   * @param {object?} opts            { parentIdx: N } → adding into pipeline[N].steps (a loop)
   */
  openAddModal(pipelineKey, preselectType = null, opts = {}) {
    this.addTarget      = pipelineKey;
    this.addNestedParent = opts.parentIdx ?? null;

    this.populateTypeSelect(pipelineKey, opts);

    if (preselectType) {
      const opt = $(`#add-step-type option[value="${preselectType}"]`);
      if (opt) $("#add-step-type").value = preselectType;
    }

    const hdr = document.querySelector(".profile-modal-title");
    if (hdr) {
      if (this.addNestedParent !== null) {
        hdr.textContent = "➕ Add step inside the loop";
      } else {
        const labels = {
          main_script:     "➕ Add step to Main script",
          post_ad_actions: "➕ Add step to Per-ad actions",
        };
        hdr.textContent = labels[pipelineKey] || "➕ Add action step";
      }
    }
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

    let html = "";

    // ── Variables info panel — shown only when adding INTO a loop ──
    // Tells the user what placeholder(s) they can use in string params.
    if (this.addNestedParent !== null && this.addNestedParent !== undefined) {
      const parent = this.pipelines[this.addTarget]?.[this.addNestedParent];
      const varName = parent?.item_var || "item";
      html += `
        <div class="modal-vars-panel">
          <div class="modal-vars-title">
            ✨ Available variables in this loop
          </div>
          <div class="modal-vars-body">
            Use these placeholders inside any text field — they'll be
            replaced with the current iteration's value when the script
            runs.
          </div>
          <div class="modal-vars-list">
            <div class="modal-var-item">
              <code class="modal-var-chip primary">{${escapeHtml(varName)}}</code>
              <span class="modal-var-desc">Current item from the loop's list.</span>
            </div>
            <div class="modal-var-item">
              <code class="modal-var-chip">{index}</code>
              <span class="modal-var-desc">Current position, <strong>starting at 1</strong>.</span>
            </div>
            <div class="modal-var-item">
              <code class="modal-var-chip">{total}</code>
              <span class="modal-var-desc">Total number of items in this loop.</span>
            </div>
          </div>
        </div>
      `;
    }

    if (!meta || !meta.params?.length) {
      html += `<div class="form-hint">No type-specific parameters.</div>`;
    } else {
      // `steps`-type params are NOT edited in the modal — they're built
      // inline inside the loop block after the step is added.
      html += meta.params
        .filter(p => p.type !== "steps")
        .map(p => this._renderModalParam(p)).join("");
      if (meta.params.some(p => p.type === "steps")) {
        html += `
          <div class="form-hint" style="margin-top: 10px; color: #a5b4fc;">
            ℹ Nested steps are added in the step's expanded view after
            creation.
          </div>`;
      }
    }

    // Common params only for per-ad pipelines, not for loop-scoped
    // pipelines (main_script / nested inside loop).
    const isLoopTarget = this.addTarget === "main_script"
                      || this.addNestedParent !== null;
    if (!isLoopTarget && this.commonParams.length) {
      html += `
        <div class="modal-section-label">Common options (per-ad context)</div>
      `;
      html += this.commonParams
        .map(p => this._renderModalParam(p, true))
        .join("");
    }

    container.innerHTML = html;

    // Pre-fill the first empty string field of a nested step with
    // {item_var} — saves the user from typing it manually and teaches
    // the pattern in context. Only touches fields that have no default.
    if (this.addNestedParent !== null && this.addNestedParent !== undefined && meta) {
      const parent = this.pipelines[this.addTarget]?.[this.addNestedParent];
      const varName = parent?.item_var || "item";
      const primaryParam = (meta.params || []).find(
        p => p.type === "text" && p.required && !p.default
      );
      if (primaryParam) {
        const input = container.querySelector(
          `[data-addparam="${primaryParam.name}"]`
        );
        if (input && !input.value) {
          input.value = `{${varName}}`;
        }
      }
    }
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
    if (p.type === "textlist") {
      const def = Array.isArray(p.default) ? p.default.join("\n") : "";
      return `
        <div class="form-group">
          <label class="form-label">${label}${p.required ? " *" : ""}</label>
          <textarea class="textarea" rows="5" ${scope}="${name}" ${ph}>${escapeHtml(def)}</textarea>
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

    try {
      document.querySelectorAll("[data-addparam]").forEach(inp => {
        const name  = inp.dataset.addparam;
        const pmeta = meta.params?.find(p => p.name === name);
        let val;
        if (inp.type === "checkbox") {
          val = inp.checked;
        } else if (inp.type === "number") {
          val = inp.value === "" ? null : parseFloat(inp.value);
        } else if (pmeta?.type === "textlist") {
          // Parse newline-separated list
          const lines = String(inp.value || "")
            .split("\n").map(s => s.trim()).filter(Boolean);
          val = lines.length ? lines : null;
        } else if (pmeta?.type === "steps") {
          val = [];   // nested steps start empty, added via the loop UI
        } else {
          val = inp.value;
        }

        if (pmeta?.required && (val === null || val === "" ||
                                (Array.isArray(val) && !val.length))) {
          throw new Error(`Field "${pmeta.label || pmeta.name}" is required`);
        }
        if (val !== null && val !== "" &&
            !(Array.isArray(val) && !val.length)) {
          step[name] = val;
        }
      });

      // For loop-type steps, make sure `steps: []` exists even if no input
      // rendered it (so the nested UI shows up immediately).
      if (type === "loop" && !Array.isArray(step.steps)) {
        step.steps = [];
      }
    } catch (e) {
      toast(e.message, true);
      return;
    }

    // Common params — only write non-default values
    document.querySelectorAll("[data-common]").forEach(inp => {
      const name = inp.dataset.common;
      const pmeta = this.commonParams.find(p => p.name === name);
      if (!pmeta) return;
      let val;
      if (inp.type === "checkbox")     val = inp.checked;
      else if (inp.type === "number")  val = inp.value === "" ? null : parseFloat(inp.value);
      else                             val = inp.value;
      if (val !== pmeta.default && val !== null && val !== "") {
        step[name] = val;
      }
    });

    // Route: top-level vs nested
    if (this.addNestedParent !== null && this.addNestedParent !== undefined) {
      this.addNested(this.addTarget, this.addNestedParent, step);
      toast(`✓ Added "${meta.label}" inside the loop`);
    } else {
      this.pipelines[this.addTarget].push(step);
      toast(`✓ Added "${meta.label}" to ${this.addTarget.replace(/_/g, " ")}`);
    }

    this.dirty = true;
    this.addNestedParent = null;
    this.closeModal("add-step-modal");
    this.renderPipelines();
  },

  // ── Save ─────────────────────────────────────────────────────

  async save() {
    const btn = $("#actions-save-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Saving…";
    try {
      await api("/api/actions/pipelines", {
        method: "POST",
        body: JSON.stringify({
          main_script:     this.pipelines.main_script,
          post_ad_actions: this.pipelines.post_ad_actions,
          // Always clear the legacy pipeline — if this user had steps
          // there, they were migrated into post_ad_actions in loadPipelines.
          on_target_domain_actions: [],
        }),
      });
      this.dirty = false;
      toast("✓ Saved");
    } catch (e) {
      toast("Save failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "💾 Save changes";
    }
  },
};
