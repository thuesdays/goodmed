// ═══════════════════════════════════════════════════════════════
// pages/domains.js — queries, my domains, target domains, block list
// ═══════════════════════════════════════════════════════════════
// All fields are bound via data-config attributes, so there's no
// custom logic here — we just load config + wire change handlers.

const Domains = {
  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // In-page nav links (e.g. the callout pointing at Scripts)
    document.querySelectorAll("[data-nav]").forEach(a => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        navigate(a.dataset.nav);
      });
    });
  },
};
