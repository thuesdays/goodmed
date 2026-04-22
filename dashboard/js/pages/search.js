// ═══════════════════════════════════════════════════════════════
// pages/search.js — all fields use data-config attrs, auto-bound
// ═══════════════════════════════════════════════════════════════

const Search = {
  async init() {
    // Ensure fresh config (in case it was changed elsewhere)
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));
  },
};
