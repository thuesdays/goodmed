// ═══════════════════════════════════════════════════════════════
// pages/actions.js — post-ad action pipeline builder
// ═══════════════════════════════════════════════════════════════

const ActionsPage = {
  // Parameter schemas for each action type
  SCHEMAS: {
    visit: {
      label: "Visit",
      params: [
        { key: "probability", type: "number", label: "Probability", min: 0, max: 1, step: 0.1, default: 1.0 },
        { key: "dwell_min",   type: "number", label: "Dwell min (s)", min: 1, default: 5 },
        { key: "dwell_max",   type: "number", label: "Dwell max (s)", min: 1, default: 15 },
      ],
    },
    dwell: {
      label: "Dwell",
      params: [
        { key: "min_sec", type: "number", label: "Min (s)", min: 0.5, step: 0.5, default: 2 },
        { key: "max_sec", type: "number", label: "Max (s)", min: 0.5, step: 0.5, default: 6 },
      ],
    },
    scroll: {
      label: "Scroll",
      params: [
        { key: "min_scrolls", type: "number", label: "Min scrolls", min: 1, default: 1 },
        { key: "max_scrolls", type: "number", label: "Max scrolls", min: 1, default: 3 },
      ],
    },
    back: {
      label: "Back",
      params: [
        { key: "delay_sec", type: "number", label: "Delay (s)", min: 0, step: 0.5, default: 1 },
      ],
    },
  },

  async init() {
    if (!configCache) await loadConfig();

    $("#add-post-ad-btn").addEventListener("click", () => this.addAction("post-ad"));
    $("#add-target-action-btn").addEventListener("click", () => this.addAction("target-action"));

    this.renderAll();
  },

  // Get the actions array for a given pipeline key
  getList(prefix) {
    configCache.actions = configCache.actions || {};
    const key = prefix === "post-ad" ? "post_ad" : "on_target_domain";
    configCache.actions[key] = configCache.actions[key] || [];
    return configCache.actions[key];
  },

  renderAll() {
    this.renderList("post-ad");
    this.renderList("target-action");
  },

  renderList(prefix) {
    const list  = $(`#${prefix}-list`);
    const count = $(`#${prefix}-count`);
    const arr   = this.getList(prefix);

    count.textContent = arr.length;

    if (!arr.length) {
      list.innerHTML = '<div class="empty-state">No actions configured</div>';
      return;
    }

    list.innerHTML = arr.map((action, idx) => this.renderItem(prefix, action, idx)).join("");
  },

  renderItem(prefix, action, idx) {
    const type    = action.type || "dwell";
    const schema  = this.SCHEMAS[type] || this.SCHEMAS.dwell;
    const enabled = action.enabled !== false;

    const typeOptions = Object.keys(this.SCHEMAS)
      .map(t => `<option value="${t}" ${t === type ? "selected" : ""}>${this.SCHEMAS[t].label}</option>`)
      .join("");

    const paramInputs = schema.params.map(p => {
      const val = action[p.key] ?? p.default;
      const attrs = [
        `type="${p.type}"`,
        p.min !== undefined ? `min="${p.min}"` : "",
        p.max !== undefined ? `max="${p.max}"` : "",
        p.step !== undefined ? `step="${p.step}"` : "",
      ].filter(Boolean).join(" ");
      return `
        <div class="action-param-group">
          <label>${escapeHtml(p.label)}</label>
          <input ${attrs} value="${val}"
                 data-prefix="${prefix}" data-idx="${idx}" data-key="${p.key}"
                 onchange="ActionsPage.onParamChange(this)">
        </div>
      `;
    }).join("");

    return `
      <div class="action-item" data-idx="${idx}">
        <div class="action-item-header">
          <span class="action-item-drag">☰</span>
          <input type="checkbox" ${enabled ? 'checked' : ''}
                 data-prefix="${prefix}" data-idx="${idx}"
                 onchange="ActionsPage.onEnabledChange(this)"
                 style="width: 16px; height: 16px; cursor: pointer;">
          <select class="select"
                  data-prefix="${prefix}" data-idx="${idx}"
                  onchange="ActionsPage.onTypeChange(this)">
            ${typeOptions}
          </select>
          <div style="flex: 1;"></div>
          <button class="action-item-delete" title="Delete"
                  onclick="ActionsPage.deleteAction('${prefix}', ${idx})">×</button>
        </div>
        <div class="action-params">${paramInputs}</div>
      </div>
    `;
  },

  onParamChange(input) {
    const { prefix, idx, key } = input.dataset;
    const arr = this.getList(prefix);
    arr[parseInt(idx)][key] = input.type === "number" ? parseFloat(input.value) : input.value;
    scheduleConfigSave();
  },

  onEnabledChange(input) {
    const { prefix, idx } = input.dataset;
    const arr = this.getList(prefix);
    arr[parseInt(idx)].enabled = input.checked;
    scheduleConfigSave();
  },

  onTypeChange(select) {
    const { prefix, idx } = select.dataset;
    const arr = this.getList(prefix);
    const newType = select.value;
    // Reset params to new type's defaults
    const defaults = {};
    this.SCHEMAS[newType].params.forEach(p => defaults[p.key] = p.default);
    arr[parseInt(idx)] = { type: newType, enabled: true, ...defaults };
    this.renderList(prefix);
    scheduleConfigSave();
  },

  deleteAction(prefix, idx) {
    if (!await confirmDialog({
      title: "Delete action", message: "Remove this action from the pipeline?",
      confirmText: "Delete", confirmStyle: "danger"
    })) return;
    const arr = this.getList(prefix);
    arr.splice(idx, 1);
    this.renderList(prefix);
    scheduleConfigSave();
  },

  addAction(prefix) {
    const defaults = {};
    this.SCHEMAS.dwell.params.forEach(p => defaults[p.key] = p.default);
    const newAction = { type: "dwell", enabled: true, ...defaults };
    const arr = this.getList(prefix);
    arr.push(newAction);
    this.renderList(prefix);
    scheduleConfigSave();
  },
};
