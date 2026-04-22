// ═══════════════════════════════════════════════════════════════
// app.js — SPA router (loads page HTML fragments on navigation)
// ═══════════════════════════════════════════════════════════════

const PAGES = {
  overview:    { html: "/pages/overview.html",    init: () => Overview.init() },
  profiles:    { html: "/pages/profiles.html",    init: () => Profiles.init() },
  search:      { html: "/pages/search.html",      init: () => Search.init() },
  proxy:       { html: "/pages/proxy.html",       init: () => ProxyPage.init() },
  profile:     { html: "/pages/profile.html",     init: () => ProfileDetail.init() },
  competitors: { html: "/pages/competitors.html", init: () => Competitors.init() },
  behavior:    { html: "/pages/behavior.html",    init: () => Behavior.init() },
  actions:     { html: "/pages/actions.html",     init: () => ActionsPage.init() },
  runs:        { html: "/pages/runs.html",        init: () => Runs.init() },
  scheduler:   { html: "/pages/scheduler.html",   init: () => Scheduler.init() },
  logs:        { html: "/pages/logs.html",        init: () => Logs.init() },
};

let currentPage = null;

async function navigate(page) {
  const route = PAGES[page];
  if (!route) return;
  currentPage = page;

  // Update sidebar
  $$(".sidebar-item").forEach(i => i.classList.toggle("active", i.dataset.page === page));

  // Load HTML fragment
  const content = $("#content");
  try {
    const html = await fetch(route.html).then(r => {
      if (!r.ok) throw new Error(`page ${page} not found`);
      return r.text();
    });
    content.innerHTML = html;
  } catch (e) {
    content.innerHTML = `<div class="empty-state">Failed to load page: ${escapeHtml(e.message)}</div>`;
    return;
  }

  // Init page logic
  try {
    if (route.init) await route.init();
  } catch (e) {
    console.error(`Page ${page} init failed:`, e);
  }
}

// Sidebar click handlers
$$(".sidebar-item").forEach(item => {
  item.addEventListener("click", () => navigate(item.dataset.page));
});

// Router init — loads config once, then navigates
(async function init() {
  await loadConfig();
  navigate("overview");
})();
