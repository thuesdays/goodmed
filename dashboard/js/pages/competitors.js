// ═══════════════════════════════════════════════════════════════
// competitors.js — Ad-intel dashboard for observed advertisers.
//
// Data pipeline:
//   /api/competitors?days=&q=       → main table + KPIs + recent
//   /api/competitors/trend?days=&top= → chart data
//   /api/competitors/sparklines?days= → per-row 7d mini-chart
//   /api/competitors/by-query?days=   → share-of-voice tab
//   /api/competitors/detail?domain=   → expandable row content
//   /api/competitors/export?format=   → CSV/JSON download
//   POST /api/competitors/add-to-list → inline actions
// ═══════════════════════════════════════════════════════════════

const Competitors = (() => {

  const state = {
    days:       7,
    search:     "",
    data:       null,        // latest /api/competitors response
    byQuery:    [],          // /api/competitors/by-query payload
    sparklines: {},          // { domain: [counts...] }
    chart:      null,        // Chart.js instance
    chartKind:  "line",      // line | stacked
    currentTab: "by-domain",
    searchTimer: null,
    expanded:   new Set(),   // set of currently expanded domain rows
    expandedDetail: {},      // { domain: detailPayload } — cache
  };

  // ─────────────────────────────────────────────────────────────
  // init
  // ─────────────────────────────────────────────────────────────
  async function init() {
    bindEvents();
    await Promise.all([reloadData(), loadSparklines()]);
  }

  function bindEvents() {
    // Search — debounce
    $("#cmp-filter-search").addEventListener("input", (e) => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.search = (e.target.value || "").trim();
        reloadData();
      }, 300);
    });

    // Period buttons — exclusive toggle
    $$(".cmp-period-btn").forEach(b => {
      b.addEventListener("click", () => {
        $$(".cmp-period-btn").forEach(x => x.classList.toggle("active", x === b));
        state.days = parseInt(b.dataset.days, 10);
        reloadData();
        loadSparklines();
      });
    });

    // Trend kind — line / stacked
    $$(".cmp-trend-btn").forEach(b => {
      b.addEventListener("click", () => {
        $$(".cmp-trend-btn").forEach(x => x.classList.toggle("active", x === b));
        state.chartKind = b.dataset.trend;
        renderChart();
      });
    });

    // Export
    $("#cmp-export-csv").addEventListener("click", () => downloadExport("csv"));
    $("#cmp-export-json").addEventListener("click", () => downloadExport("json"));

    // Tabs
    $("#cmp-tabs").addEventListener("click", (e) => {
      const t = e.target.closest(".fp-tab");
      if (t) switchTab(t.dataset.tab);
    });
  }

  function switchTab(name) {
    state.currentTab = name;
    $$(".fp-tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    $$(".fp-tabpane").forEach(p => p.classList.toggle("active", p.dataset.tabpane === name));
    // Lazy load by-query tab on first visit
    if (name === "by-query" && !state.byQuery.length) loadByQuery();
  }

  // ─────────────────────────────────────────────────────────────
  // Data fetchers
  // ─────────────────────────────────────────────────────────────
  async function reloadData() {
    try {
      const qs = new URLSearchParams();
      if (state.days) qs.set("days", state.days);
      if (state.search) qs.set("q", state.search);
      state.data = await api(`/api/competitors?${qs.toString()}`);
      renderKPIs();
      renderByDomain();
      renderRecent();
      renderTabBadges();
      await loadTrend();
    } catch (e) {
      console.error("competitors reload:", e);
      toast("Competitors load failed: " + e.message, true);
    }
  }

  async function loadTrend() {
    try {
      const qs = new URLSearchParams({ days: String(state.days || 30), top: "8" });
      const trend = await api(`/api/competitors/trend?${qs}`);
      renderChart(trend);
    } catch (e) { console.warn("trend:", e); }
  }

  async function loadSparklines() {
    try {
      const qs = new URLSearchParams({ days: "7" });
      const resp = await api(`/api/competitors/sparklines?${qs}`);
      state.sparklines = resp.data || {};
      // Re-render the domain table to pick up fresh sparklines
      if (state.data) renderByDomain();
    } catch (e) { console.warn("sparklines:", e); }
  }

  async function loadByQuery() {
    try {
      const qs = new URLSearchParams({ days: String(state.days || 30) });
      const resp = await api(`/api/competitors/by-query?${qs}`);
      state.byQuery = resp.queries || [];
      renderByQuery();
    } catch (e) { console.error("by-query:", e); }
  }

  async function downloadExport(fmt) {
    const qs = new URLSearchParams({ format: fmt });
    if (state.days) qs.set("days", state.days);
    if (state.search) qs.set("q", state.search);
    // Simple: navigate to the URL, browser handles Content-Disposition
    window.location.href = `/api/competitors/export?${qs.toString()}`;
  }

  // ─────────────────────────────────────────────────────────────
  // Render: KPIs
  // ─────────────────────────────────────────────────────────────
  function renderKPIs() {
    const d = state.data || {};
    $("#cmp-kpi-records").textContent  = d.total_records ?? "—";
    $("#cmp-kpi-domains").textContent  = d.unique_domains ?? "—";
    $("#cmp-kpi-records-sub").textContent =
      state.days ? `last ${state.days === 1 ? "24h" : state.days + "d"}`
                 : "all time";
    $("#cmp-kpi-domains-sub").textContent = `all time: ${d.all_time_unique ?? "—"}`;

    const k = d.kpis || {};
    $("#cmp-kpi-new").textContent      = k.new      ?? 0;
    $("#cmp-kpi-active").textContent   = k.active   ?? 0;
    $("#cmp-kpi-quieting").textContent = k.quieting ?? 0;
  }

  // ─────────────────────────────────────────────────────────────
  // Render: By-domain table
  // ─────────────────────────────────────────────────────────────
  function renderByDomain() {
    const tbody = $("#cmp-tbody");
    const by = state.data?.by_domain || [];
    if (!by.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="dense-empty-cell">
        ${state.search ? `No competitors match "${escapeHtml(state.search)}"` :
                         "No competitors recorded yet"}
      </td></tr>`;
      return;
    }
    tbody.innerHTML = by.map(d => {
      const expanded = state.expanded.has(d.domain);
      const spark = state.sparklines[d.domain] || [];
      const queryCount = (d.queries || []).length;
      const activityLabel = {
        new:      "NEW",
        active:   "ACTIVE",
        quieting: "QUIETING",
        steady:   "STEADY",
      }[d.activity] || "—";

      const main = `
        <tr class="cmp-row ${expanded ? "expanded" : ""}"
            data-domain="${escapeHtml(d.domain)}">
          <td class="cmp-expand-cell">
            <button class="cmp-expand-btn" aria-label="Toggle detail">${expanded ? "▾" : "▸"}</button>
          </td>
          <td class="cmp-domain-cell">
            <a href="https://${escapeHtml(d.domain)}" target="_blank" rel="noopener">
              <strong>${escapeHtml(d.domain)}</strong>
            </a>
          </td>
          <td><span class="cmp-badge cmp-badge-${escapeHtml(d.activity)}">${activityLabel}</span></td>
          <td class="num"><span class="cmp-count">${d.mentions ?? 0}</span></td>
          <td class="cmp-spark-cell">${renderSparkline(spark)}</td>
          <td class="num">
            ${d.actions_ran || 0}
            ${d.actions_skipped ? `<div class="muted" style="font-size:10px;">${d.actions_skipped} skip</div>` : ""}
          </td>
          <td class="num">${queryCount}</td>
          <td class="muted" style="font-family: ui-monospace, monospace; font-size: 11px;">
            ${escapeHtml(d.last_seen ? timeAgo(d.last_seen) : "—")}
          </td>
          <td class="cmp-action-cell">
            <button class="cmp-action-btn cmp-action-target" data-add="target"
                    data-domain="${escapeHtml(d.domain)}"
                    title="Add to target_domains — enables on-target action pipeline">🎯</button>
            <button class="cmp-action-btn cmp-action-my" data-add="my"
                    data-domain="${escapeHtml(d.domain)}"
                    title="Add to my_domains — stop treating as a competitor">🏠</button>
            <button class="cmp-action-btn cmp-action-block" data-add="block"
                    data-domain="${escapeHtml(d.domain)}"
                    title="Add to block_domains — monitor ignores entirely">🚫</button>
          </td>
        </tr>
      `;
      const detail = expanded ? `
        <tr class="cmp-detail-row" data-detail-for="${escapeHtml(d.domain)}">
          <td colspan="9"><div class="cmp-detail-body" id="cmp-detail-${CSS.escape(d.domain)}">
            <div class="muted">Loading detail…</div>
          </div></td>
        </tr>
      ` : "";
      return main + detail;
    }).join("");

    // Wire row interactions
    tbody.querySelectorAll(".cmp-row").forEach(row => {
      const domain = row.dataset.domain;
      row.querySelector(".cmp-expand-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        toggleExpand(domain);
      });
      row.querySelector(".cmp-domain-cell a").addEventListener("click", (e) => {
        e.stopPropagation(); /* let the link work, don't toggle */
      });
      row.querySelectorAll(".cmp-action-btn").forEach(b =>
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          addToList(domain, b.dataset.add, b);
        }));
    });

    // Populate any expanded-and-not-yet-loaded detail rows
    for (const dom of state.expanded) {
      if (!state.expandedDetail[dom]) loadDetail(dom);
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Sparkline — inline SVG, no external deps
  // ─────────────────────────────────────────────────────────────
  function renderSparkline(values) {
    if (!values || !values.length) return '<span class="muted">—</span>';
    const w = 90, h = 26, pad = 2;
    const max = Math.max(1, ...values);
    const step = (w - pad * 2) / Math.max(1, values.length - 1);
    const points = values.map((v, i) =>
      `${(pad + i * step).toFixed(1)},${(h - pad - (v / max) * (h - pad * 2)).toFixed(1)}`
    ).join(" ");
    // Soft gradient fill under the line for visual weight
    const fill = `M ${pad},${h - pad} L ${points.replace(/,/g, " ").split(" ").join(" L ")} L ${w - pad},${h - pad} Z`
      .replace(/L\s+M/, "M");
    const lastVal = values[values.length - 1];
    const peakClass = lastVal === max ? "cmp-spark-peak" : "";
    return `
      <svg class="cmp-spark ${peakClass}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">
        <polyline class="cmp-spark-line" fill="none" points="${points}"></polyline>
        <circle class="cmp-spark-dot" cx="${(pad + (values.length - 1) * step).toFixed(1)}"
                cy="${(h - pad - (lastVal / max) * (h - pad * 2)).toFixed(1)}" r="2"/>
      </svg>`;
  }

  // ─────────────────────────────────────────────────────────────
  // Expandable rows
  // ─────────────────────────────────────────────────────────────
  function toggleExpand(domain) {
    if (state.expanded.has(domain)) state.expanded.delete(domain);
    else state.expanded.add(domain);
    renderByDomain();
  }

  async function loadDetail(domain) {
    try {
      const qs = new URLSearchParams({ domain, days: String(state.days || 30) });
      const d = await api(`/api/competitors/detail?${qs}`);
      state.expandedDetail[domain] = d;
      renderDetailInto(domain, d);
    } catch (e) {
      console.error("detail load:", e);
    }
  }

  function renderDetailInto(domain, d) {
    const host = document.getElementById("cmp-detail-" + CSS.escape(domain));
    if (!host) return;
    const titlesHtml = (d.titles || []).length
      ? d.titles.map(t => `
          <li>
            <span class="cmp-detail-count">${t.n}×</span>
            <span class="cmp-detail-title">${escapeHtml(t.title || "(empty)")}</span>
            <span class="muted" style="font-size: 11px;">· ${timeAgo(t.last_seen)}</span>
          </li>
        `).join("")
      : '<li class="muted">No titles recorded.</li>';

    const urlsHtml = (d.urls || []).length
      ? d.urls.map(u => `
          <li>
            <span class="cmp-detail-count">${u.n}×</span>
            <code>${escapeHtml(u.display_url)}</code>
          </li>
        `).join("")
      : '<li class="muted">No display URLs recorded.</li>';

    const queriesHtml = (d.queries || []).length
      ? d.queries.map(q => `
          <li>
            <span class="cmp-detail-count">${q.n}×</span>
            <span>${escapeHtml(q.query || "(empty)")}</span>
          </li>
        `).join("")
      : '<li class="muted">No queries recorded.</li>';

    host.innerHTML = `
      <div class="cmp-detail-grid">
        <div>
          <div class="cmp-detail-header">Ad titles <span class="muted">top ${Math.min(8, d.titles?.length || 0)}</span></div>
          <ul class="cmp-detail-list">${titlesHtml}</ul>
        </div>
        <div>
          <div class="cmp-detail-header">Display URLs</div>
          <ul class="cmp-detail-list">${urlsHtml}</ul>
        </div>
        <div>
          <div class="cmp-detail-header">Matched queries</div>
          <ul class="cmp-detail-list">${queriesHtml}</ul>
        </div>
      </div>
    `;
  }

  // ─────────────────────────────────────────────────────────────
  // Inline actions — add domain to search.* lists
  // ─────────────────────────────────────────────────────────────
  async function addToList(domain, list, btn) {
    const label = { target: "🎯 target", my: "🏠 my", block: "🚫 block" }[list];
    const originalText = btn.textContent;
    btn.disabled = true;
    try {
      const resp = await api("/api/competitors/add-to-list", {
        method: "POST",
        body: JSON.stringify({ domain, list }),
      });
      btn.textContent = "✓";
      btn.classList.add("cmp-action-done");
      toast(resp.already
        ? `${domain} is already in ${label} list`
        : `✓ Added ${domain} to ${label} list`);
      setTimeout(() => {
        btn.textContent = originalText;
        btn.classList.remove("cmp-action-done");
        btn.disabled = false;
      }, 1600);
    } catch (e) {
      btn.disabled = false;
      toast("Add failed: " + e.message, true);
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Render: By-query tab (share of voice)
  // ─────────────────────────────────────────────────────────────
  function renderByQuery() {
    const host = $("#cmp-byquery-list");
    if (!state.byQuery.length) {
      host.innerHTML = '<div class="dense-empty" style="padding: 32px 20px;">No query data in this window.</div>';
      return;
    }
    // Colour palette cycling for the bars
    const palette = ["#818cf8", "#34d399", "#fbbf24", "#f87171", "#60a5fa",
                     "#c084fc", "#f472b6", "#2dd4bf"];
    host.innerHTML = state.byQuery.map(q => {
      const bars = q.competitors.map((c, i) => `
        <div class="cmp-sov-row" title="${escapeHtml(c.domain)} · ${c.mentions} mentions">
          <div class="cmp-sov-label">${escapeHtml(c.domain)}</div>
          <div class="cmp-sov-bar-wrap">
            <div class="cmp-sov-bar"
                 style="width: ${c.pct}%; background: ${palette[i % palette.length]};"></div>
          </div>
          <div class="cmp-sov-pct">${c.pct}%</div>
          <div class="cmp-sov-count muted">${c.mentions}</div>
        </div>
      `).join("");
      return `
        <div class="cmp-sov-card">
          <div class="cmp-sov-header">
            <div class="cmp-sov-query">${escapeHtml(q.query)}</div>
            <div class="cmp-sov-total muted">${q.total} impressions</div>
          </div>
          <div class="cmp-sov-body">${bars}</div>
        </div>
      `;
    }).join("");
  }

  // ─────────────────────────────────────────────────────────────
  // Render: Recent ads tab
  // ─────────────────────────────────────────────────────────────
  function renderRecent() {
    const tbody = $("#cmp-recent-tbody");
    const rows = state.data?.recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="dense-empty-cell">No records in this period.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(r => {
      const url = r.google_click_url || "";
      const short = url.length > 90 ? url.slice(0, 90) + "…" : url;
      return `
        <tr>
          <td class="muted" style="font-family: ui-monospace, monospace; font-size: 11px;">
            ${fmtTimestamp(r.timestamp)}
          </td>
          <td class="truncate" title="${escapeHtml(r.query || '')}">${escapeHtml(r.query || '')}</td>
          <td class="cmp-domain-cell">
            <a href="https://${escapeHtml(r.domain)}" target="_blank" rel="noopener">
              <strong>${escapeHtml(r.domain)}</strong>
            </a>
          </td>
          <td class="truncate" title="${escapeHtml(r.title || '')}">${escapeHtml(r.title || '')}</td>
          <td class="truncate">
            <a href="${escapeHtml(url || '#')}" target="_blank" rel="noopener"
               class="muted" style="font-size: 11px;">${escapeHtml(short)}</a>
          </td>
        </tr>`;
    }).join("");
  }

  // ─────────────────────────────────────────────────────────────
  // Render: Chart.js trend chart
  // ─────────────────────────────────────────────────────────────
  function renderChart(payload) {
    const trend = payload || state._lastTrend;
    if (payload) state._lastTrend = payload;
    if (!trend) return;
    const canvas = document.getElementById("cmp-trend-chart");
    const empty  = document.getElementById("cmp-chart-empty");
    if (!trend.series || !trend.series.length) {
      canvas.style.display = "none";
      empty.style.display  = "block";
      return;
    }
    canvas.style.display = "block";
    empty.style.display  = "none";

    // Destroy previous instance when switching chart kind / data
    if (state.chart) { state.chart.destroy(); state.chart = null; }

    const palette = ["#818cf8", "#34d399", "#fbbf24", "#f87171", "#60a5fa",
                     "#c084fc", "#f472b6", "#2dd4bf"];
    const datasets = trend.series.map((s, i) => ({
      label: s.domain,
      data:  s.counts,
      borderColor:     palette[i % palette.length],
      backgroundColor: state.chartKind === "stacked"
        ? palette[i % palette.length] + "aa"
        : palette[i % palette.length] + "22",
      fill: state.chartKind === "stacked" ? true : false,
      tension: 0.3,
      pointRadius: 2,
      pointHoverRadius: 4,
      borderWidth: 2,
    }));

    state.chart = new Chart(canvas, {
      type: "line",
      data: { labels: trend.dates, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: {
            labels: {
              color: "#94a3b8",
              boxWidth: 10, boxHeight: 10,
              font: { size: 11 },
            },
            position: "bottom",
          },
          tooltip: {
            backgroundColor: "#0b0f14",
            titleColor: "#f3f4f6",
            bodyColor: "#cbd5e1",
            borderColor: "#334155",
            borderWidth: 1,
          },
        },
        scales: {
          x: {
            ticks: { color: "#64748b", font: { size: 10 } },
            grid:  { color: "rgba(148,163,184,0.08)" },
          },
          y: {
            stacked: state.chartKind === "stacked",
            beginAtZero: true,
            ticks: { color: "#64748b", font: { size: 10 }, precision: 0 },
            grid:  { color: "rgba(148,163,184,0.08)" },
          },
        },
      },
    });
  }

  function renderTabBadges() {
    $("#cmp-tab-domain-badge").textContent = state.data?.by_domain?.length ?? "—";
    $("#cmp-tab-query-badge").textContent  = state.byQuery.length || "—";
    $("#cmp-tab-recent-badge").textContent = state.data?.recent?.length ?? "—";
  }

  function teardown() {
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  }

  return { init, teardown };
})();
