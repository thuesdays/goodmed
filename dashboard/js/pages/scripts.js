// ═══════════════════════════════════════════════════════════════
// pages/scripts.js — scripts library + flow editor
//
// Two modes in one page:
//
//   LIBRARY   (default)  — grid of all saved scripts. Each card
//                          shows name/description/step count/list
//                          of profiles using it. Click a card to
//                          enter editor mode for that script.
//
//   EDITOR    (scoped)   — unified-flow builder for a single
//                          script. Has its own Save, Export,
//                          Delete, Make-default buttons.
//
// State transitions happen in-page (no route change). `currentScript`
// is null in library mode, or the loaded {id, name, description,
// flow} dict in editor mode.
//
// Backend endpoints used:
//   GET/POST   /api/scripts
//   GET/PUT/DELETE  /api/scripts/<id>
//   GET        /api/actions/catalog
//   GET        /api/actions/condition-kinds
// ═══════════════════════════════════════════════════════════════

const ScriptsPage = {
  catalog:        [],
  conditionKinds: [],
  scripts:        [],     // library list (summary only)
  currentScript:  null,   // {id, name, description, flow, is_default} when editing
  flow:           [],     // current editing flow
  selection:      null,
  dirty:          false,
  _arrowsRAF:     null,

  // ── Lifecycle ────────────────────────────────────────────────

  async init() {
    await this.loadCatalog();
    await this.loadConditionKinds();
    await this.loadLibrary();

    this.wireLibraryHeader();
    this.wireEditorHeader();
    this.wirePalette();
    this.wireKeyboard();
    this.wireVarPicker();
    this.wireNewScriptModal();

    window.addEventListener("beforeunload", e => {
      if (this.dirty) { e.preventDefault(); e.returnValue = ""; }
    });
    window.addEventListener("resize", () => this._scheduleArrowsRedraw());

    this._showLibrary();
  },

  teardown() {
    if (this._arrowsRAF) cancelAnimationFrame(this._arrowsRAF);
    const vp = $("#var-picker");
    if (vp) vp.style.display = "none";
  },

  // ── Loaders (shared) ─────────────────────────────────────────

  async loadCatalog() {
    try {
      const resp = await api("/api/actions/catalog");
      const items = Array.isArray(resp) ? resp : (resp.types || []);
      items.forEach(c => {
        if (!c.category) c.category = "other";
        c.is_container = c.is_container ||
          ["if", "foreach_ad", "foreach", "loop"].includes(c.type);
      });
      this.catalog = items;
      this.renderPalette();
    } catch (e) {
      console.error("catalog load:", e);
      toast("Failed to load action catalog", true);
    }
  },

  async loadConditionKinds() {
    try {
      const resp = await api("/api/actions/condition-kinds");
      this.conditionKinds = resp.kinds || [];
    } catch (e) {
      this.conditionKinds = [{ kind: "always", label: "Always run" }];
    }
  },

  async loadLibrary() {
    try {
      const resp = await api("/api/scripts");
      this.scripts = resp.scripts || [];
      this.renderLibrary();
    } catch (e) {
      console.error("scripts load:", e);
      toast("Failed to load scripts", true);
    }
  },

  // ── View mode switching ──────────────────────────────────────

  _showLibrary() {
    this.currentScript = null;
    this.selection = null;
    this.dirty = false;
    $("#scripts-library-view").style.display = "";
    $("#scripts-editor-view").style.display  = "none";
    this.loadLibrary();   // refresh list
  },

  async _showEditor(scriptId) {
    // Load full script (with flow) from server
    try {
      const resp = await api(`/api/scripts/${scriptId}`);
      const sc = resp.script;
      if (!sc) throw new Error("Script not found");
      this.currentScript = sc;
      this.flow = sc.flow || [];
      this.selection = null;
      this.dirty = false;
      $("#scripts-library-view").style.display = "none";
      $("#scripts-editor-view").style.display  = "";

      // Populate header fields
      $("#editor-name-input").value = sc.name || "";
      $("#editor-desc-input").value = sc.description || "";
      $("#editor-default-badge").style.display = sc.is_default ? "" : "none";
      $("#editor-default-btn").style.display   = sc.is_default ? "none" : "";
      $("#editor-delete-btn").style.display    = sc.is_default ? "none" : "";

      this.renderFlow();
    } catch (e) {
      toast(`Could not open script: ${e.message}`, true);
    }
  },

  // ═══════════════════════════════════════════════════════════════
  // LIBRARY VIEW
  // ═══════════════════════════════════════════════════════════════

  renderLibrary() {
    const grid = $("#scripts-library-grid");
    if (!this.scripts.length) {
      grid.innerHTML = `<div class="library-empty">
        No scripts yet. Click <strong>+ New script</strong> to create one.
      </div>`;
      return;
    }
    grid.innerHTML = this.scripts.map(s => this._renderLibraryCard(s)).join("");
    // Card click → open editor
    grid.querySelectorAll(".library-card").forEach(card => {
      card.addEventListener("click", () => {
        const id = Number(card.dataset.scriptId);
        this._showEditor(id);
      });
    });
  },

  _renderLibraryCard(s) {
    const updated = s.updated_at
      ? this._formatRelative(s.updated_at)
      : "—";
    const pCount = s.profile_count || 0;
    const pLabel = pCount === 1 ? "1 profile" : `${pCount} profiles`;
    const desc = s.description || "(no description)";
    return `
      <div class="library-card" data-script-id="${s.id}">
        <div class="library-card-header">
          <div class="library-card-name">${escapeHtml(s.name)}</div>
          ${s.is_default
            ? `<span class="library-card-default" title="Default script">DEFAULT</span>`
            : ""}
        </div>
        <div class="library-card-desc">${escapeHtml(desc)}</div>
        <div class="library-card-stats">
          <span class="library-card-stat">
            <strong>${s.step_count || 0}</strong> steps
          </span>
          <span class="library-card-stat"
                title="Profiles using this script">
            <strong>${pCount}</strong> ${pLabel.replace(/^\d+ /, "")}
          </span>
          <span class="library-card-stat library-card-time">${updated}</span>
        </div>
      </div>`;
  },

  /** Turn an ISO timestamp into "3h ago" / "Apr 24" etc. Quick-n-dirty. */
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

  wireLibraryHeader() {
    $("#library-new-btn").addEventListener("click", () => {
      this._openNewScriptModal();
    });
    $("#library-import-btn").addEventListener("click", () => {
      $("#library-import-file").click();
    });
    $("#library-import-file").addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      e.target.value = "";
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const parsed = JSON.parse(reader.result);
          const flow = Array.isArray(parsed) ? parsed
                     : Array.isArray(parsed.flow) ? parsed.flow
                     : null;
          if (!flow) throw new Error("No flow array in file");
          // Validate
          const check = (steps, path = "") => {
            for (let i = 0; i < steps.length; i++) {
              const s = steps[i];
              if (!s || typeof s !== "object" || !s.type) {
                throw new Error(`Invalid step at ${path}[${i}]`);
              }
              if (Array.isArray(s.steps))      check(s.steps, `${path}[${i}].steps`);
              if (Array.isArray(s.then_steps)) check(s.then_steps, `${path}[${i}].then_steps`);
              if (Array.isArray(s.else_steps)) check(s.else_steps, `${path}[${i}].else_steps`);
            }
          };
          check(flow, "flow");

          // Auto-name from file metadata or filename
          const suggestedName = parsed._meta?.name
            || file.name.replace(/\.json$/i, "");
          this._openNewScriptModal({
            name: suggestedName,
            description: parsed._meta?.description || "",
            flow,
          });
        } catch (err) {
          toast(`Import failed: ${err.message}`, true);
        }
      };
      reader.readAsText(file);
    });
  },

  wireNewScriptModal() {
    document.querySelectorAll('[data-close="new-script-modal"]').forEach(el => {
      el.addEventListener("click", () => this._closeNewScriptModal());
    });
    $("#new-script-create-btn").addEventListener("click",
      () => this._confirmNewScript());
  },

  _openNewScriptModal(prefill = {}) {
    const modal = $("#new-script-modal");
    $("#new-script-name").value = prefill.name || "";
    $("#new-script-desc").value = prefill.description || "";
    modal._pendingFlow = prefill.flow || [];
    modal.style.display = "";
    setTimeout(() => $("#new-script-name").focus(), 30);
  },

  _closeNewScriptModal() {
    const modal = $("#new-script-modal");
    modal.style.display = "none";
    modal._pendingFlow = null;
  },

  async _confirmNewScript() {
    const modal = $("#new-script-modal");
    const name = $("#new-script-name").value.trim();
    const desc = $("#new-script-desc").value.trim();
    if (!name) {
      toast("Name is required", true);
      return;
    }
    const btn = $("#new-script-create-btn");
    btn.disabled = true;
    try {
      const resp = await api("/api/scripts", {
        method: "POST",
        body: JSON.stringify({
          name, description: desc,
          flow: modal._pendingFlow || [],
        }),
      });
      this._closeNewScriptModal();
      toast(`✓ Created "${name}"`);
      // Jump straight into editor for the new script
      await this._showEditor(resp.id);
    } catch (e) {
      toast(`Create failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
    }
  },

  // ═══════════════════════════════════════════════════════════════
  // EDITOR VIEW (unified flow builder)
  // ═══════════════════════════════════════════════════════════════

  wireEditorHeader() {
    $("#editor-back-btn").addEventListener("click", (e) => {
      e.preventDefault();
      if (this.dirty && !confirm("Discard unsaved changes?")) return;
      this._showLibrary();
    });
    $("#editor-save-btn").addEventListener("click", () => this.save());
    $("#editor-reload-btn").addEventListener("click", () => {
      if (this.dirty && !confirm("Discard unsaved changes?")) return;
      this._showEditor(this.currentScript.id);
    });
    $("#editor-delete-btn").addEventListener("click", () => this._deleteScript());
    $("#editor-default-btn").addEventListener("click", () => this._makeDefault());
    $("#editor-export-btn").addEventListener("click", () => this._exportScript());
    $("#inspector-close-btn").addEventListener("click", () => {
      this.selection = null;
      this.renderInspector();
      this.highlightSelection();
    });

    // Name + description — live save-on-blur (no auto-save, just mark dirty)
    $("#editor-name-input").addEventListener("input", () => this._markDirty());
    $("#editor-desc-input").addEventListener("input", () => this._markDirty());
  },

  async save() {
    if (!this.currentScript) return;
    const btn = $("#editor-save-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Saving…";
    try {
      const name = $("#editor-name-input").value.trim();
      const desc = $("#editor-desc-input").value.trim();
      if (!name) {
        toast("Name is required", true);
        return;
      }
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name, description: desc, flow: this.flow,
        }),
      });
      this.currentScript.name = name;
      this.currentScript.description = desc;
      this.dirty = false;
      toast("✓ Saved");
    } catch (e) {
      toast(`Save failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "💾 Save";
    }
  },

  async _deleteScript() {
    if (!this.currentScript) return;
    if (!confirm(
      `Delete "${this.currentScript.name}"? Profiles using this ` +
      `script will fall back to the default.`
    )) return;
    try {
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "DELETE",
      });
      toast("✓ Deleted");
      this._showLibrary();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, true);
    }
  },

  async _makeDefault() {
    if (!this.currentScript) return;
    try {
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_default: true }),
      });
      this.currentScript.is_default = 1;
      $("#editor-default-badge").style.display = "";
      $("#editor-default-btn").style.display = "none";
      $("#editor-delete-btn").style.display   = "none";
      toast(`✓ "${this.currentScript.name}" is now the default`);
    } catch (e) {
      toast(`Could not set default: ${e.message}`, true);
    }
  },

  _exportScript() {
    if (!this.currentScript) return;
    const blob = new Blob(
      [JSON.stringify({
        _meta: {
          format:      "ghost-shell-flow",
          version:     1,
          name:        this.currentScript.name,
          description: this.currentScript.description,
          exported_at: new Date().toISOString(),
        },
        flow: this.flow,
      }, null, 2)],
      { type: "application/json" }
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeName = (this.currentScript.name || "script")
      .replace(/[^a-z0-9_-]+/gi, "_").toLowerCase();
    a.download = `${safeName}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  },

  // ── Keyboard ─────────────────────────────────────────────────

  wireKeyboard() {
    document.addEventListener("keydown", (e) => {
      // Only when in editor mode and a step is selected
      if (!this.currentScript || !this.selection) return;
      const t = e.target;
      if (t && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        this._removeAt(this.selection.path);
        this.selection = null;
        this._markDirty();
        this.renderFlow();
      } else if (e.key === "Escape") {
        this.selection = null;
        this.renderInspector();
        this.highlightSelection();
      }
    });
  },

  // ── PALETTE ───────────────────────────────────────────────────

  renderPalette() {
    const body = $("#palette-body");
    if (!this.catalog.length) {
      body.innerHTML = `<div class="palette-empty">No actions available</div>`;
      return;
    }
    const order = ["flow", "navigation", "interaction", "timing",
                   "data", "external", "input", "power", "other"];
    const grouped = {};
    order.forEach(c => grouped[c] = []);
    this.catalog.forEach(a => {
      const cat = order.includes(a.category) ? a.category : "other";
      grouped[cat].push(a);
    });
    const groupLabels = {
      flow:        ["#22d3ee", "Flow control"],
      navigation:  ["#60a5fa", "Navigation"],
      interaction: ["#a78bfa", "Interaction"],
      timing:      ["#f59e0b", "Timing"],
      data:        ["#34d399", "Data"],
      external:    ["#fb7185", "External"],
      input:       ["#94a3b8", "Input"],
      power:       ["#94a3b8", "Power"],
      other:       ["#94a3b8", "Other"],
    };
    const renderItem = (a) => `
      <div class="palette-item" draggable="true"
           data-type="${escapeHtml(a.type)}"
           data-category="${escapeHtml(a.category)}"
           data-search="${escapeHtml(((a.label || "") + " " + a.type).toLowerCase())}">
        <div class="palette-item-icon">${this._iconFor(a)}</div>
        <div class="palette-item-body">
          <div class="palette-item-label">${escapeHtml(a.label || a.type)}</div>
          <div class="palette-item-desc">${escapeHtml(a.description || "")}</div>
        </div>
      </div>`;
    const html = order
      .filter(cat => grouped[cat].length)
      .map(cat => {
        const [color, label] = groupLabels[cat];
        return `
          <div class="palette-group">
            <div class="palette-group-label">
              <span class="palette-group-dot" style="background:${color}"></span>
              ${label}
            </div>
            ${grouped[cat].map(renderItem).join("")}
          </div>`;
      }).join("");
    body.innerHTML = html;
  },

  wirePalette() {
    $("#palette-search").addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $("#palette-body").querySelectorAll(".palette-item").forEach(it => {
        const hay = it.dataset.search || "";
        it.style.display = (!q || hay.includes(q)) ? "" : "none";
      });
    });
    $("#palette-body").addEventListener("click", (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      this._addStep({ containerPath: [] }, item.dataset.type);
    });
    $("#palette-body").addEventListener("dragstart", (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      item.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("application/x-gs-palette",
        JSON.stringify({ type: item.dataset.type }));
    });
    $("#palette-body").addEventListener("dragend", (e) => {
      e.target.closest(".palette-item")?.classList.remove("is-dragging");
    });
  },

  // ── CANVAS (flow editor) ─────────────────────────────────────

  renderFlow() {
    $("#stat-total-count").textContent = this._countSteps(this.flow);
    const root = $("#canvas-flow");
    root.innerHTML = this._renderStepList(this.flow, [], "root")
                   + this._renderAddButton([]);
    this.wireCanvasInteractions();
    this.renderInspector();
    this.highlightSelection();
    this._scheduleArrowsRedraw();
  },

  _countSteps(steps) {
    let n = 0;
    for (const s of (steps || [])) {
      n++;
      n += this._countSteps(s.steps);
      n += this._countSteps(s.then_steps);
      n += this._countSteps(s.else_steps);
    }
    return n;
  },

  _renderStepList(steps, basePath, containerKey) {
    if (!steps || !steps.length) return "";
    return steps.map((s, i) => {
      const path = [...basePath, { key: containerKey, idx: i }];
      return this._renderStep(s, path);
    }).join("");
  },

  _renderStep(step, path) {
    const meta = this.catalog.find(c => c.type === step.type);
    const isContainer = meta?.is_container ||
      ["if", "foreach_ad", "foreach", "loop"].includes(step.type);
    if (isContainer) return this._renderContainer(step, path, meta);
    return this._renderSimpleStep(step, path, meta);
  },

  _renderSimpleStep(step, path, meta) {
    const label    = meta ? (meta.label || step.type) : step.type;
    const category = meta?.category || "other";
    const enabled  = step.enabled !== false;
    const idx      = path[path.length - 1].idx;
    return `
      <div class="flow-step ${enabled ? '' : 'is-disabled'}"
           data-path="${this._encodePath(path)}"
           data-category="${category}"
           data-type="${escapeHtml(step.type)}"
           draggable="true">
        <div class="flow-step-head">
          <div class="flow-step-num">${idx + 1}</div>
          <div class="flow-step-icon">${this._iconFor(meta || {type: step.type})}</div>
          <div class="flow-step-label">${escapeHtml(label)}</div>
          <div class="flow-step-actions">
            <button class="btn-icon" data-action="toggle"
                    title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '⏸' : '▶'}</button>
            <button class="btn-icon" data-action="duplicate" title="Duplicate">⎘</button>
            <button class="btn-icon" data-action="remove" title="Remove">✕</button>
          </div>
        </div>
        <div class="flow-step-body">${this._buildChips(step, meta)}</div>
      </div>`;
  },

  _renderContainer(step, path, meta) {
    const label = meta ? (meta.label || step.type) : step.type;
    const idx   = path[path.length - 1].idx;
    const summary = this._buildContainerSummary(step, meta);
    const enabled = step.enabled !== false;

    let bodyHtml;
    if (step.type === "if") {
      bodyHtml = `
        <div class="flow-container-body ${(step.then_steps || []).length === 0 ? 'is-empty' : ''}"
             data-container-path="${this._encodePath(path)}:then_steps">
          <div class="container-subregion-label then-label">Then</div>
          ${this._renderStepList(step.then_steps || [], path, "then_steps")
            || this._renderEmptyBodyMarker("then")}
          ${this._renderAddButton(path, "then_steps")}

          ${(step.else_steps && step.else_steps.length) || this._elseOpen(path)
            ? `<div class="container-subregion-label else-label">Else</div>
               ${this._renderStepList(step.else_steps || [], path, "else_steps")
                 || this._renderEmptyBodyMarker("else")}
               ${this._renderAddButton(path, "else_steps")}`
            : `<button class="flow-add-btn"
                       data-action="add-else"
                       data-path="${this._encodePath(path)}">+ Add else branch</button>`}
        </div>`;
    } else {
      const nested = step.steps || [];
      bodyHtml = `
        <div class="flow-container-body ${nested.length === 0 ? 'is-empty' : ''}"
             data-container-path="${this._encodePath(path)}:steps">
          ${this._renderStepList(nested, path, "steps")
            || this._renderEmptyBodyMarker()}
          ${this._renderAddButton(path, "steps")}
        </div>`;
    }
    return `
      <div class="flow-container ${enabled ? '' : 'is-disabled'}"
           data-path="${this._encodePath(path)}"
           data-ctype="${escapeHtml(step.type)}"
           draggable="true">
        <div class="flow-container-head">
          <div class="flow-step-num">${idx + 1}</div>
          <div class="flow-step-icon">${this._iconFor(meta || {type: step.type})}</div>
          <div class="flow-container-title">
            <span class="flow-container-title-label">${escapeHtml(label)}</span>
            ${summary ? `<span class="flow-container-summary">${summary}</span>` : ''}
          </div>
          <div class="flow-step-actions">
            <button class="btn-icon" data-action="toggle"
                    title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '⏸' : '▶'}</button>
            <button class="btn-icon" data-action="duplicate" title="Duplicate">⎘</button>
            <button class="btn-icon" data-action="remove" title="Remove">✕</button>
          </div>
        </div>
        ${bodyHtml}
      </div>`;
  },

  _renderEmptyBodyMarker(kind = "") {
    return `<div class="container-body-empty">
      Empty — drag actions here${kind ? ` to run when <strong>${kind}</strong>` : ""}.
    </div>`;
  },

  _elseOpen(path) {
    const step = this._getAt(path);
    return step && Array.isArray(step.else_steps) &&
           (step.else_steps.length > 0 || step._else_expanded);
  },

  _renderAddButton(basePath, subKey = "steps") {
    return `
      <button class="flow-add-btn"
              data-action="add-step"
              data-path="${this._encodePath(basePath)}"
              data-subkey="${subKey}">+ Add step</button>`;
  },

  _buildChips(step, meta) {
    if (!meta) {
      return `<span class="flow-step-chip chip-prob">UNKNOWN TYPE</span>`;
    }
    const chips = [];
    const params = (meta.params || [])
      .filter(p => !["steps", "then_steps", "else_steps", "condition"].includes(p.name));
    let shown = 0;
    for (const p of params) {
      if (shown >= 3) break;
      const v = step[p.name];
      if (v === undefined || v === null || v === "" || v === p.default) continue;
      const display = this._paramDisplay(p, v);
      if (!display) continue;
      chips.push(
        `<span class="flow-step-chip">
           <span class="flow-step-chip-label">${escapeHtml(p.label || p.name)}:</span>
           <code>${escapeHtml(display)}</code>
         </span>`
      );
      shown++;
    }
    const prob = step.probability !== undefined ? Number(step.probability) : 1.0;
    if (prob < 1.0) {
      chips.push(`<span class="flow-step-chip chip-prob">p = ${prob.toFixed(2)}</span>`);
    }
    if (!chips.length) {
      chips.push(`<span class="flow-step-chip"><code>${escapeHtml(step.type)}</code></span>`);
    }
    return chips.join("");
  },

  _buildContainerSummary(step, meta) {
    if (step.type === "if") {
      const c = step.condition || {};
      const kind = c.kind || "always";
      const kindMeta = this.conditionKinds.find(k => k.kind === kind);
      const label = kindMeta?.label || kind;
      return `<code>${c.negate ? "NOT " : ""}${escapeHtml(label)}</code>`;
    }
    if (step.type === "foreach_ad") {
      const n = (step.limit ? `first ${step.limit}` : "all");
      return `<code>foreach ${n} ad(s)</code>`;
    }
    if (step.type === "foreach" || step.type === "loop") {
      const items = step.items;
      const itemVar = step.item_var || "item";
      if (typeof items === "string") {
        const lines = items.split("\n").filter(l => l.trim()).length;
        return `<code>{${escapeHtml(itemVar)}} in ${lines} item(s)</code>`;
      }
      if (Array.isArray(items)) {
        return `<code>{${escapeHtml(itemVar)}} in ${items.length} item(s)</code>`;
      }
    }
    return "";
  },

  _paramDisplay(p, v) {
    if (p.type === "bool") return v ? "✓" : "✗";
    if (Array.isArray(v))  return `${v.length} item${v.length === 1 ? "" : "s"}`;
    if (typeof v === "string") {
      if (p.type === "textlist") {
        const lines = v.split("\n").filter(l => l.trim()).length;
        return `${lines} line${lines === 1 ? "" : "s"}`;
      }
      return v.length > 24 ? v.slice(0, 22) + "…" : v;
    }
    return String(v);
  },

  wireCanvasInteractions() {
    const canvas = $("#canvas-flow");
    canvas.onclick = (e) => {
      const actionBtn = e.target.closest(".flow-step-actions button");
      if (actionBtn) {
        const card = actionBtn.closest("[data-path]");
        const path = this._decodePath(card.dataset.path);
        this._handleAction(path, actionBtn.dataset.action);
        e.stopPropagation();
        return;
      }
      const addBtn = e.target.closest('.flow-add-btn[data-action="add-step"]');
      if (addBtn) {
        const basePath = this._decodePath(addBtn.dataset.path);
        const subKey = addBtn.dataset.subkey || "steps";
        this._openTypePicker({ basePath, subKey });
        e.stopPropagation();
        return;
      }
      const elseBtn = e.target.closest('.flow-add-btn[data-action="add-else"]');
      if (elseBtn) {
        const path = this._decodePath(elseBtn.dataset.path);
        const step = this._getAt(path);
        if (step && !step.else_steps) step.else_steps = [];
        step._else_expanded = true;
        this._markDirty();
        this.renderFlow();
        return;
      }
      const card = e.target.closest("[data-path]");
      if (card && canvas.contains(card)) {
        const path = this._decodePath(card.dataset.path);
        this.selection = { path };
        this.renderInspector();
        this.highlightSelection();
      }
    };
    canvas.querySelectorAll("[data-path][draggable]").forEach(card => {
      card.addEventListener("dragstart", (e) => {
        if (e.target.closest(".flow-step-actions")) { e.preventDefault(); return; }
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("application/x-gs-move",
          JSON.stringify({ path: this._decodePath(card.dataset.path) }));
        card.classList.add("is-dragging");
        e.stopPropagation();
      });
      card.addEventListener("dragend", () => card.classList.remove("is-dragging"));
    });
    const zones = [canvas, ...canvas.querySelectorAll(".flow-container-body")];
    zones.forEach(zone => {
      zone.addEventListener("dragover", (e) => {
        if (!e.dataTransfer.types.includes("application/x-gs-palette") &&
            !e.dataTransfer.types.includes("application/x-gs-move")) return;
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("drop-active");
      });
      zone.addEventListener("dragleave", (e) => {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        zone.classList.remove("drop-active");
      });
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove("drop-active");
        this._handleDrop(zone, e);
      });
    });
  },

  _handleDrop(zone, e) {
    const dt = e.dataTransfer;
    let target;
    if (zone.id === "canvas-flow") {
      target = { basePath: [], subKey: "root" };
    } else {
      const containerCard = zone.closest(".flow-container");
      if (!containerCard) return;
      const cPath = this._decodePath(containerCard.dataset.path);
      const cStep = this._getAt(cPath);
      let subKey = "steps";
      if (cStep?.type === "if") {
        const elseLabelEl = [...zone.querySelectorAll(".container-subregion-label.else-label")][0];
        if (elseLabelEl && e.clientY > elseLabelEl.getBoundingClientRect().top) {
          subKey = "else_steps";
        } else {
          subKey = "then_steps";
        }
      }
      target = { basePath: cPath, subKey };
    }
    const palRaw = dt.getData("application/x-gs-palette");
    if (palRaw) {
      try {
        const { type } = JSON.parse(palRaw);
        this._addStep({ containerPath: target.basePath,
                        subKey: target.subKey }, type);
      } catch {}
      return;
    }
    const moveRaw = dt.getData("application/x-gs-move");
    if (moveRaw) {
      try {
        const { path: srcPath } = JSON.parse(moveRaw);
        this._moveStep(srcPath, target);
      } catch {}
    }
  },

  _handleAction(path, action) {
    if (action === "remove") {
      this._removeAt(path);
      this.selection = null;
    } else if (action === "toggle") {
      const step = this._getAt(path);
      if (step) step.enabled = step.enabled === false;
    } else if (action === "duplicate") {
      const step = this._getAt(path);
      if (!step) return;
      const copy = JSON.parse(JSON.stringify(step));
      delete copy._else_expanded;
      this._insertAfter(path, copy);
    }
    this._markDirty();
    this.renderFlow();
  },

  // Path utilities
  _encodePath(path) {
    return path.map(s => `${s.key}:${s.idx}`).join("/");
  },
  _decodePath(enc) {
    if (!enc) return [];
    return enc.split("/").map(seg => {
      const [key, idxStr] = seg.split(":");
      return { key, idx: Number(idxStr) };
    });
  },
  _getContainerArray(path) {
    if (path.length === 0) return this.flow;
    let arr = this.flow;
    for (let i = 0; i < path.length - 1; i++) {
      const step = arr[path[i].idx];
      const childKey = path[i + 1].key;
      arr = step[childKey] || [];
    }
    return arr;
  },
  _getAt(path) {
    if (path.length === 0) return null;
    const arr = this._getContainerArray(path);
    return arr[path[path.length - 1].idx];
  },
  _removeAt(path) {
    const arr = this._getContainerArray(path);
    arr.splice(path[path.length - 1].idx, 1);
  },
  _insertAfter(path, step) {
    const arr = this._getContainerArray(path);
    arr.splice(path[path.length - 1].idx + 1, 0, step);
  },

  _addStep({ containerPath, subKey = "root" }, type) {
    const meta = this.catalog.find(c => c.type === type);
    if (!meta) return;
    const step = this._defaultStep(meta);
    let arr;
    if (subKey === "root" || containerPath.length === 0) {
      arr = this.flow;
    } else {
      const container = this._getAt(containerPath);
      if (!container) return;
      container[subKey] = container[subKey] || [];
      arr = container[subKey];
    }
    arr.push(step);
    const newIdx = arr.length - 1;
    if (subKey === "root" || containerPath.length === 0) {
      this.selection = { path: [{ key: "root", idx: newIdx }] };
    } else {
      this.selection = { path: [...containerPath,
                                { key: subKey, idx: newIdx }] };
    }
    this._markDirty();
    this.renderFlow();
  },

  _moveStep(srcPath, target) {
    const srcArr = this._getContainerArray(srcPath);
    const [step] = srcArr.splice(srcPath[srcPath.length - 1].idx, 1);
    if (!step) return;
    let dstArr;
    if (target.subKey === "root" || target.basePath.length === 0) {
      dstArr = this.flow;
    } else {
      const c = this._getAt(target.basePath);
      if (!c) { srcArr.push(step); return; }
      c[target.subKey] = c[target.subKey] || [];
      dstArr = c[target.subKey];
    }
    dstArr.push(step);
    this.selection = null;
    this._markDirty();
    this.renderFlow();
  },

  _defaultStep(meta) {
    const step = { type: meta.type, enabled: true };
    (meta.params || []).forEach(p => {
      if (p.default !== undefined &&
          !["steps", "then_steps", "else_steps"].includes(p.name)) {
        step[p.name] = p.default;
      }
    });
    if (meta.is_container) {
      if (meta.type === "if") {
        step.condition = { kind: "always" };
        step.then_steps = [];
      } else {
        step.steps = [];
      }
    }
    return step;
  },

  _markDirty() { this.dirty = true; },

  // Selection
  highlightSelection() {
    $("#canvas-flow").querySelectorAll(".is-selected")
      .forEach(el => el.classList.remove("is-selected"));
    const panes = document.querySelector(".scripts-panes");
    const inspector = $("#scripts-inspector");
    if (!this.selection) {
      $("#inspector-close-btn").style.display = "none";
      if (inspector) inspector.style.display = "none";
      panes?.classList.add("inspector-hidden");
      return;
    }
    if (inspector) inspector.style.display = "";
    panes?.classList.remove("inspector-hidden");
    $("#inspector-close-btn").style.display = "";
    const enc = this._encodePath(this.selection.path);
    $("#canvas-flow").querySelector(`[data-path="${enc}"]`)
      ?.classList.add("is-selected");
  },

  // ── INSPECTOR (identical to v3 — full params, condition builder) ──

  renderInspector() {
    const body  = $("#inspector-body");
    const title = $("#inspector-title");
    if (!this.selection) {
      title.textContent = "Inspector";
      body.innerHTML = "";
      return;
    }
    const step = this._getAt(this.selection.path);
    if (!step) { this.selection = null; return this.renderInspector(); }
    const meta = this.catalog.find(c => c.type === step.type);
    title.textContent = meta?.label || step.type;

    const sections = [];
    sections.push(`
      <div class="inspector-section">
        <div class="inspector-badge-row">
          <span class="inspector-type-tag">${escapeHtml(step.type)}</span>
          <span class="inspector-cat-tag" data-category="${meta?.category || 'other'}">${
            escapeHtml(meta?.category || 'other')}</span>
        </div>
        ${meta?.description
          ? `<div class="inspector-field-hint">${escapeHtml(meta.description)}</div>`
          : ""}
      </div>`);

    sections.push(`
      <div class="inspector-section">
        <div class="inspector-section-title">Execution</div>
        <label class="inspector-check-row">
          <input type="checkbox" data-insp="enabled"
                 ${step.enabled !== false ? "checked" : ""}>
          <div>
            <div>Enabled</div>
            <span class="inspector-check-row-hint">Disabled steps skip at runtime.</span>
          </div>
        </label>
        <div class="inspector-field" style="margin-top: 8px;">
          <div class="inspector-field-label">
            Probability
            <span style="color: var(--text-muted); font-weight: 400;">
              ${Number(step.probability ?? 1.0).toFixed(2)}
            </span>
          </div>
          <input type="range" min="0" max="1" step="0.05"
                 data-insp="probability"
                 value="${Number(step.probability ?? 1.0)}"
                 style="width: 100%;">
          <div class="inspector-field-hint">
            Fraction of runs that execute this step.
          </div>
        </div>
      </div>`);

    if (step.type === "if") {
      sections.push(this._renderConditionBuilder(step));
    }

    const params = (meta?.params || [])
      .filter(p => !["steps", "then_steps", "else_steps", "condition"]
                       .includes(p.name));
    if (params.length) {
      sections.push(`
        <div class="inspector-section">
          <div class="inspector-section-title">Parameters</div>
          ${params.map(p => this._renderParam(p, step)).join("")}
        </div>`);
    }
    body.innerHTML = sections.join("");
    this._wireInspectorInputs();
  },

  _renderConditionBuilder(step) {
    const cond = step.condition || { kind: "always" };
    const kindMeta = this.conditionKinds.find(k => k.kind === cond.kind);
    const groups = {};
    this.conditionKinds.forEach(k => {
      (groups[k.group || "simple"] ||= []).push(k);
    });
    const groupOrder = ["simple", "ads", "page", "vars"];
    const optHtml = groupOrder
      .filter(g => groups[g])
      .map(g => `
        <optgroup label="${g}">
          ${groups[g].map(k =>
            `<option value="${escapeHtml(k.kind)}"
                     ${k.kind === cond.kind ? "selected" : ""}>${
              escapeHtml(k.label)}</option>`
          ).join("")}
        </optgroup>`).join("");
    const fields = kindMeta?.fields || [];
    const fieldsHtml = fields.map(f => {
      const val = cond[f.name] ?? f.default ?? "";
      const ph  = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
      const needsVars = f.type === "text";
      return `
        <div class="inspector-field ${needsVars ? 'field-has-vars' : ''}">
          <div class="inspector-field-label">${escapeHtml(f.label || f.name)}</div>
          <input type="${f.type === 'number' ? 'number' : 'text'}"
                 class="input" data-cond-field="${escapeHtml(f.name)}"
                 value="${escapeHtml(String(val))}" ${ph}>
        </div>`;
    }).join("");
    return `
      <div class="inspector-section">
        <div class="inspector-section-title">Condition</div>
        <div class="cond-builder">
          <div class="cond-kind-row">
            <select class="select" data-cond-kind>${optHtml}</select>
          </div>
          <label class="cond-negate-check">
            <input type="checkbox" data-cond-negate ${cond.negate ? "checked" : ""}>
            Negate (run when condition is FALSE)
          </label>
          ${fields.length ? `<div class="cond-fields">${fieldsHtml}</div>` : ""}
        </div>
      </div>`;
  },

  _renderParam(p, step) {
    const val = step[p.name] ?? p.default ?? "";
    const label = escapeHtml(p.label || p.name);
    const name  = escapeHtml(p.name);
    const hint  = p.hint
      ? `<div class="inspector-field-hint">${escapeHtml(p.hint)}</div>`
      : "";
    if (p.type === "bool") {
      return `
        <label class="inspector-check-row">
          <input type="checkbox" data-param="${name}" ${val ? "checked" : ""}>
          <div>${label}${hint}</div>
        </label>`;
    }
    if (p.type === "number" || p.type === "int" || p.type === "float") {
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <input type="number" class="input" data-param="${name}"
                 value="${escapeHtml(String(val))}"
                 ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>
          ${hint}
        </div>`;
    }
    if (p.type === "select" && Array.isArray(p.options)) {
      const opts = p.options.map(o => {
        const ov = o.value ?? o;
        const ol = o.label ?? o;
        return `<option value="${escapeHtml(String(ov))}"
                        ${String(ov) === String(val) ? "selected" : ""}>${escapeHtml(String(ol))}</option>`;
      }).join("");
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <select class="select" data-param="${name}">${opts}</select>
          ${hint}
        </div>`;
    }
    if (p.type === "textarea" || p.type === "textlist") {
      return `
        <div class="inspector-field field-has-vars">
          <div class="inspector-field-label">${label}</div>
          <textarea class="input" data-param="${name}" rows="${p.type === "textlist" ? 6 : 4}"
                    ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>${escapeHtml(String(val))}</textarea>
          ${hint}
        </div>`;
    }
    return `
      <div class="inspector-field field-has-vars">
        <div class="inspector-field-label">${label}</div>
        <input type="text" class="input" data-param="${name}"
               value="${escapeHtml(String(val))}"
               ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>
        ${hint}
      </div>`;
  },

  _wireInspectorInputs() {
    const body = $("#inspector-body");
    body.querySelectorAll("[data-insp]").forEach(input => {
      const update = () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        const key = input.dataset.insp;
        const val = input.type === "checkbox" ? input.checked
                  : (input.type === "range" ? parseFloat(input.value)
                  : input.value);
        if (val === "" || val === false) delete step[key];
        else step[key] = val;
        this._markDirty();
        this.renderFlow();
      };
      input.addEventListener("change", update);
      if (input.type === "range") {
        input.addEventListener("input", () => {
          const lbl = input.previousElementSibling;
          const num = lbl?.querySelector("span");
          if (num) num.textContent = Number(input.value).toFixed(2);
        });
      }
    });
    const kindSel = body.querySelector("[data-cond-kind]");
    if (kindSel) {
      kindSel.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = { kind: kindSel.value };
        this._markDirty();
        this.renderInspector();
        this.renderFlow();
      });
    }
    body.querySelectorAll("[data-cond-negate]").forEach(cb => {
      cb.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = step.condition || { kind: "always" };
        step.condition.negate = cb.checked;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll("[data-cond-field]").forEach(input => {
      input.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = step.condition || { kind: "always" };
        const key = input.dataset.condField;
        step.condition[key] = input.type === "number"
          ? (input.value === "" ? "" : Number(input.value))
          : input.value;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll("[data-param]").forEach(input => {
      input.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        const key = input.dataset.param;
        let val;
        if (input.type === "checkbox") val = input.checked;
        else if (input.type === "number") val = input.value === "" ? "" : Number(input.value);
        else val = input.value;
        if (val === "" || val === null) delete step[key];
        else step[key] = val;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll(".field-has-vars input, .field-has-vars textarea")
      .forEach(input => {
        input.addEventListener("focus", () => this._openVarPicker(input));
        input.addEventListener("click", () => this._openVarPicker(input));
      });
  },

  // Var picker
  wireVarPicker() {
    const vp = $("#var-picker");
    if (!vp) return;
    document.addEventListener("mousedown", (e) => {
      if (vp.style.display === "none") return;
      if (vp.contains(e.target)) return;
      if (e.target.matches(
        ".field-has-vars input, .field-has-vars textarea")) return;
      vp.style.display = "none";
    });
  },
  _openVarPicker(input) {
    const vp = $("#var-picker");
    if (!vp) return;
    const rect = input.getBoundingClientRect();
    vp.style.left = `${Math.min(rect.left, window.innerWidth - 280)}px`;
    vp.style.top  = `${rect.bottom + 4}px`;
    vp.style.display = "";
    const vars = this._availableVarsForPath(this.selection?.path || []);
    const body = $("#var-picker-body");
    body.innerHTML = vars.map(g => `
      <div class="var-picker-group-label">${escapeHtml(g.label)}</div>
      ${g.items.map(v => `
        <div class="var-picker-item" data-var="${escapeHtml(v.path)}">
          <code>{${escapeHtml(v.path)}}</code>
          <span class="var-picker-item-desc">${escapeHtml(v.desc)}</span>
        </div>`).join("")}
    `).join("");
    body.onclick = (e) => {
      const item = e.target.closest(".var-picker-item");
      if (!item) return;
      const token = `{${item.dataset.var}}`;
      if (input.tagName === "INPUT" || input.tagName === "TEXTAREA") {
        const start = input.selectionStart ?? input.value.length;
        const end   = input.selectionEnd ?? input.value.length;
        input.value = input.value.slice(0, start) + token + input.value.slice(end);
        input.focus();
        input.selectionStart = input.selectionEnd = start + token.length;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
      vp.style.display = "none";
    };
  },
  _availableVarsForPath(path) {
    const groups = [];
    const ancestors = [];
    for (let i = 1; i < path.length; i++) {
      const pref = path.slice(0, i);
      const step = this._getAt(pref);
      if (step) ancestors.push(step);
    }
    const hasForeachAd = ancestors.some(s => s.type === "foreach_ad");
    const hasForeach = ancestors.filter(s => s.type === "foreach" || s.type === "loop");
    if (hasForeachAd) {
      groups.push({
        label: "Current ad",
        items: [
          { path: "ad.domain",          desc: "hostname of the ad" },
          { path: "ad.title",           desc: "ad headline" },
          { path: "ad.clean_url",       desc: "destination URL" },
          { path: "ad.display_url",     desc: "display URL shown on SERP" },
          { path: "ad.google_click_url",desc: "Google tracking URL" },
          { path: "ad.is_target",       desc: "true if target-domain" },
          { path: "ad.ad_format",       desc: "text/shopping_carousel/pla_grid" },
        ],
      });
    }
    if (hasForeach.length) {
      hasForeach.forEach(fe => {
        const v = fe.item_var || "item";
        groups.push({
          label: `Loop variable (${v})`,
          items: [{ path: v, desc: "current iteration value" }],
        });
      });
    }
    groups.push({
      label: "Ads list",
      items: [{ path: "ads.count", desc: "number of ads in context" }],
    });
    groups.push({
      label: "Context",
      items: [
        { path: "query",   desc: "current query string" },
        { path: "profile", desc: "running profile name" },
      ],
    });
    groups.push({
      label: "Saved variables",
      items: [{ path: "var.<n>", desc: "from save_var / extract_text" }],
    });
    return groups;
  },

  // Type picker
  _openTypePicker(target) {
    const modal = $("#type-picker-modal");
    const search = $("#type-picker-search");
    const list  = $("#type-picker-list");
    const render = (q = "") => {
      const qLo = q.trim().toLowerCase();
      const groups = { flow: [], navigation: [], interaction: [],
                       timing: [], data: [], external: [],
                       input: [], power: [], other: [] };
      this.catalog.forEach(c => {
        const cat = groups[c.category] ? c.category : "other";
        if (!qLo ||
            (c.label || "").toLowerCase().includes(qLo) ||
            c.type.toLowerCase().includes(qLo)) {
          groups[cat].push(c);
        }
      });
      const order = ["flow", "navigation", "interaction", "timing",
                     "data", "external", "input", "power", "other"];
      list.innerHTML = order.filter(k => groups[k].length).map(k => `
        <div class="palette-group-label" style="padding: 12px 4px 4px;">${k}</div>
        ${groups[k].map(a => `
          <div class="palette-item" data-type="${escapeHtml(a.type)}"
               data-category="${escapeHtml(a.category)}">
            <div class="palette-item-icon">${this._iconFor(a)}</div>
            <div class="palette-item-body">
              <div class="palette-item-label">${escapeHtml(a.label || a.type)}</div>
              <div class="palette-item-desc">${escapeHtml(a.description || "")}</div>
            </div>
          </div>`).join("")}
      `).join("") || `<div class="palette-empty">No matches</div>`;
    };
    render();
    modal.style.display = "";
    search.value = "";
    setTimeout(() => search.focus(), 30);
    search.oninput = (e) => render(e.target.value);
    list.onclick = (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      this._addStep({
        containerPath: target.basePath,
        subKey:        target.subKey,
      }, item.dataset.type);
      this._closeTypePicker();
    };
    modal.querySelectorAll("[data-close]").forEach(el => {
      el.onclick = () => this._closeTypePicker();
    });
    const onKey = (e) => { if (e.key === "Escape") this._closeTypePicker(); };
    modal._pickerKey = onKey;
    document.addEventListener("keydown", onKey);
  },
  _closeTypePicker() {
    const modal = $("#type-picker-modal");
    modal.style.display = "none";
    if (modal._pickerKey) {
      document.removeEventListener("keydown", modal._pickerKey);
      modal._pickerKey = null;
    }
  },

  // Arrows
  _scheduleArrowsRedraw() {
    if (this._arrowsRAF) cancelAnimationFrame(this._arrowsRAF);
    this._arrowsRAF = requestAnimationFrame(() => {
      this._arrowsRAF = null;
      this._redrawArrows();
    });
  },
  _redrawArrows() {
    const svg  = $("#flow-arrows");
    const wrap = svg?.parentElement;
    if (!svg || !wrap) return;
    const wrapRect = wrap.getBoundingClientRect();
    svg.setAttribute("width",  wrap.scrollWidth);
    svg.setAttribute("height", wrap.scrollHeight);
    svg.innerHTML = "";
    const collectPairs = (parent) => {
      const kids = [...parent.children].filter(
        c => c.matches(".flow-step, .flow-container")
      );
      const pairs = [];
      for (let i = 0; i < kids.length - 1; i++) {
        pairs.push([kids[i], kids[i + 1]]);
      }
      const bodies = [...parent.querySelectorAll(":scope > .flow-container > .flow-container-body")];
      bodies.forEach(b => pairs.push(...collectPairs(b)));
      return pairs;
    };
    const canvas = $("#canvas-flow");
    const pairs = collectPairs(canvas);
    const svgNS = "http://www.w3.org/2000/svg";
    for (const [a, b] of pairs) {
      const r1 = a.getBoundingClientRect();
      const r2 = b.getBoundingClientRect();
      const x1 = r1.left + r1.width / 2 - wrapRect.left + wrap.scrollLeft;
      const y1 = r1.bottom - wrapRect.top + wrap.scrollTop;
      const x2 = r2.left + r2.width / 2 - wrapRect.left + wrap.scrollLeft;
      const y2 = r2.top - wrapRect.top + wrap.scrollTop;
      const midY = (y1 + y2) / 2;
      const d = `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2 - 6}`;
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "flow-arrow-line");
      svg.appendChild(path);
      const head = document.createElementNS(svgNS, "polygon");
      head.setAttribute("class", "flow-arrow-head");
      const hx = x2, hy = y2 - 2;
      head.setAttribute("points",
        `${hx - 4},${hy - 5} ${hx + 4},${hy - 5} ${hx},${hy}`);
      svg.appendChild(head);
    }
  },

  // Icons
  _iconFor(meta) {
    const type = (meta.type || "").toLowerCase();
    const map = {
      search_query: "🔎", catch_ads: "🎣", pause: "⏸", dwell: "⏳",
      rotate_ip: "🔄", refresh: "↻", click_ad: "🖱", click_selector: "🎯",
      visit: "🌐", visit_url: "🌐", new_tab: "🆕", close_tab: "✕",
      switch_tab: "⇆", back: "◀", read: "📖", hover: "👉",
      scroll: "📜", scroll_to_bottom: "⬇", type: "⌨", press_key: "⌨",
      fill_form: "📝", wait_for: "⏱", wait_for_url: "🧭",
      loop: "🔁", foreach: "🔁", foreach_ad: "🎯",
      if: "⎇", break: "🚫", continue: "↪",
      extract_text: "📋", save_var: "💾", http_request: "📡",
      move_random: "🖱", random_delay: "⏳", open_url: "🌐",
    };
    return map[type] || "·";
  },
};

const Scripts = ScriptsPage;
