// ═══════════════════════════════════════════════════════════════
// app.js — SPA router (loads page HTML fragments on navigation)
// ═══════════════════════════════════════════════════════════════

const PAGES = {
  overview:    { html: "/pages/overview.html",    init: () => Overview.init() },
  profiles:    { html: "/pages/profiles.html",    init: () => Profiles.init(),    teardown: () => Profiles.teardown?.() },
  groups:      { html: "/pages/groups.html",      init: () => Groups.init(),      teardown: () => Groups.teardown?.() },
  // "Domains" page (was "Search") — queries + my-domains + target-domains
  domains:     { html: "/pages/domains.html",     init: () => Domains.init() },
  proxy:       { html: "/pages/proxy.html",       init: () => ProxyPage.init() },
  profile:     { html: "/pages/profile.html",     init: () => ProfileDetail.init() },
  competitors: { html: "/pages/competitors.html", init: () => Competitors.init() },
  behavior:    { html: "/pages/behavior.html",    init: () => Behavior.init() },
  // "Scripts" page (was "Actions") — pipeline/script builder
  scripts:     { html: "/pages/scripts.html",     init: () => ScriptsPage.init() },
  runs:        { html: "/pages/runs.html",        init: () => Runs.init() },
  traffic:     { html: "/pages/traffic.html",     init: () => Traffic.init(),     teardown: () => Traffic.teardown?.() },
  scheduler:   { html: "/pages/scheduler.html",   init: () => Scheduler.init() },
  logs:        { html: "/pages/logs.html",        init: () => Logs.init(),        teardown: () => Logs.teardown?.() },
  settings:    { html: "/pages/settings.html",    init: () => Settings.init() },

  // Legacy aliases — redirect old bookmarks to new page names.
  // Keep for one release, then drop.
  search:      { html: "/pages/domains.html",     init: () => Domains.init() },
  actions:     { html: "/pages/scripts.html",     init: () => ScriptsPage.init() },
};

let currentPage = null;

async function navigate(page) {
  const route = PAGES[page];
  if (!route) return;

  // Let the outgoing page clean up intervals, EventSources, etc.
  // Convention: page modules can expose a teardown() on the same object
  // they init from. Missing teardown is fine — most pages are stateless.
  if (currentPage && currentPage !== page) {
    const prev = PAGES[currentPage];
    if (prev && typeof prev.teardown === "function") {
      try { await prev.teardown(); } catch (e) { console.warn(e); }
    }
  }

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

// Brand click → Overview (treat brand like a nav item)
const brand = document.querySelector(".sidebar-brand[data-page]");
if (brand) {
  brand.addEventListener("click", () => navigate(brand.dataset.page));
  brand.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      navigate(brand.dataset.page);
    }
  });
}

// Router init — loads config once, fetches build info, then navigates
(async function init() {
  await loadConfig();
  // Populate build badge in sidebar (non-blocking, silent on failure)
  fetch("/api/stats").then(r => r.json()).then(s => {
    const b = s?.build_info;
    if (!b?.chromium_build) return;
    const badge = document.getElementById("sidebar-build-badge");
    const text  = document.getElementById("sidebar-build-text");
    if (badge && text) {
      // Show the full engine version (149.0.7805.0 etc). That is the
      // actual Chromium source this binary was compiled from.
      text.textContent = b.chromium_build_full || `${b.chromium_build}.x`;
      badge.title =
        `Chromium engine:   ${b.chromium_build_full || b.chromium_build}\n` +
        `UA spoof range:    Chrome ${b.spoof_min || b.chrome_pool?.[b.chrome_pool.length-1] || "?"}` +
        ` – ${b.spoof_max || b.chrome_spoof || "?"}`;
      badge.style.display = "inline-flex";
    }
  }).catch(() => {});
  navigate("overview");
})();
