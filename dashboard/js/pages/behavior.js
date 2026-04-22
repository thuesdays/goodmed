// ═══════════════════════════════════════════════════════════════
// pages/behavior.js
// ═══════════════════════════════════════════════════════════════

const Behavior = {
  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));
  },
};
