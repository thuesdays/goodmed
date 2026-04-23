// ═══════════════════════════════════════════════════════════════
// pages/overview.js — hero stats + 7-day chart + recent activity +
// top competitors + per-profile health rollup
// ═══════════════════════════════════════════════════════════════

const Overview = {
  chart: null,

  async init() {
    // Load everything in parallel
    await Promise.all([
      this.loadHeadlineStats(),
      this.loadRecentActivity(),
      this.loadTopCompetitors(),
      this.loadProfileHealth(),
      this.loadTrafficCard(),
    ]);

    // Clicking the traffic card navigates to the full page. We attach
    // here rather than in HTML because navigate() is a JS function —
    // cleaner than an onclick handler that calls it.
    const card = document.getElementById("stat-traffic-card");
    if (card) {
      const go = () => navigate("traffic");
      card.addEventListener("click", go);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    }
  },

  // ── Traffic card (24h rollup, clicks through to full page) ──────
  //
  // Queries the same /api/traffic/summary endpoint the Traffic page uses.
  // We show bytes + request count, and flag "heavy" traffic so users
  // with paid proxies notice before their next bill. Heavy threshold
  // is 1 GB/day per profile pool — tuned against asocks pricing (~$3/GB
  // for residential). Users with different price tiers can adjust the
  // threshold in Settings (not yet wired — see traffic.heavy_threshold_gb).

  async loadTrafficCard() {
    const bytesEl = document.getElementById("stat-traffic-bytes");
    const subEl   = document.getElementById("stat-traffic-sub");
    const chipEl  = document.getElementById("stat-traffic-chip");
    if (!bytesEl) return;
    try {
      const s = await api("/api/traffic/summary?hours=24");
      const b = s.total_bytes || 0;
      // formatBytes is the canonical helper from utils.js — loaded
      // before page scripts, always available on window.
      bytesEl.textContent = formatBytes(b);
      subEl.textContent   = `${(s.total_requests || 0).toLocaleString()} requests`;
      // Heavy = >1 GB in 24h. Chip is hint, not a blocker.
      if (chipEl) {
        chipEl.style.display = b > 1024 * 1024 * 1024 ? "" : "none";
      }
    } catch (e) {
      bytesEl.textContent = "—";
      subEl.textContent   = "no data";
      console.warn("Traffic card load:", e);
    }
  },

  // ── Headline stats (hero + big cards + chart) ────────────────

  async loadHeadlineStats() {
    try {
      const stats = await api("/api/stats");

      // Hero title — friendly greeting
      const hour = new Date().getHours();
      const greet = hour < 5 ? "Good night" :
                    hour < 12 ? "Good morning" :
                    hour < 17 ? "Good afternoon" :
                    hour < 22 ? "Good evening" : "Good night";
      $("#ov-hero-title").textContent = `${greet}!`;
      $("#ov-hero-sub").textContent =
        `${stats.total_profiles || 0} profile(s) active · ` +
        `${stats.total_competitors || 0} competitors tracked`;

      // Hero stats (24h)
      const d = stats.daily || [];
      const today = d[d.length - 1] || {};
      const yday  = d[d.length - 2] || {};

      $("#hero-searches").textContent = today.searches ?? 0;
      this._renderTrend("hero-searches-trend",
                        today.searches ?? 0, yday.searches ?? 0);

      $("#hero-ads").textContent = today.ads ?? 0;
      this._renderTrend("hero-ads-trend",
                        today.ads ?? 0, yday.ads ?? 0);

      $("#hero-captchas").textContent = today.captchas ?? 0;
      this._renderTrend("hero-captchas-trend",
                        today.captchas ?? 0, yday.captchas ?? 0,
                        true);   // inverted: less captchas = better

      // Actions (24h): clicks / visits / reads ran
      const a24 = stats.actions_24h || {};
      const ranH = a24.actions_ran || 0;
      const skH  = a24.actions_skipped || 0;
      $("#hero-actions").textContent = ranH;
      // Build breakdown by type: "5 click_ad · 3 visit_link …"
      const byType = a24.by_type || {};
      const breakdown = Object.entries(byType)
        .slice(0, 3)
        .map(([t, n]) => `${n} ${t.replace(/_/g, " ")}`)
        .join(" · ");
      const trendEl = $("#hero-actions-trend");
      if (trendEl) {
        if (breakdown) {
          trendEl.textContent = breakdown + (skH ? ` · ${skH} skipped` : "");
        } else if (skH) {
          trendEl.textContent = `${skH} skipped (no clicks yet)`;
        } else {
          trendEl.textContent = "no actions yet";
        }
      }

      // Success rate (24h)
      const totalToday = (today.searches ?? 0) + (today.empty ?? 0)
                       + (today.captchas ?? 0);
      const rateToday = totalToday > 0
        ? (today.searches ?? 0) / totalToday
        : 0;
      const totalYday = (yday.searches ?? 0) + (yday.empty ?? 0)
                      + (yday.captchas ?? 0);
      const rateYday = totalYday > 0
        ? (yday.searches ?? 0) / totalYday
        : 0;

      $("#hero-rate").textContent = (rateToday * 100).toFixed(0) + "%";
      this._renderTrend("hero-rate-trend",
                        rateToday, rateYday, false, "%");

      // Totals block
      $("#stat-searches").textContent    = stats.total_searches;
      $("#stat-ads").textContent         = stats.total_ads ?? stats.total_searches;
      $("#stat-competitors").textContent = stats.total_competitors;
      $("#stat-profiles").textContent    = stats.total_profiles;

      // All-time actions performed
      const at = stats.actions_total || {};
      const ranAll = at.actions_ran || 0;
      const skAll  = at.actions_skipped || 0;
      const errAll = at.actions_errored || 0;
      const statActions = $("#stat-actions");
      if (statActions) statActions.textContent = ranAll;
      const statActionsSub = $("#stat-actions-sub");
      if (statActionsSub) {
        const parts = [];
        if (skAll)  parts.push(`${skAll} skipped`);
        if (errAll) parts.push(`${errAll} errored`);
        statActionsSub.textContent = parts.length
          ? parts.join(" · ")
          : "no skipped / errored";
      }

      const badge = $("#badge-competitors");
      if (badge) badge.textContent = stats.total_competitors;

      this.renderChart(stats.daily || []);
    } catch (e) {
      console.error("overview stats:", e);
      $("#ov-hero-sub").textContent = "Failed to load statistics";
    }
  },

  _renderTrend(elId, current, previous, invert = false, suffix = "") {
    const el = $("#" + elId);
    if (!el) return;
    if (previous == null || previous === 0) {
      if (current > 0) {
        el.textContent = "new";
        el.className = "ov-hero-stat-trend up";
      } else {
        el.textContent = "—";
        el.className = "ov-hero-stat-trend flat";
      }
      return;
    }
    const diff = current - previous;
    if (diff === 0) {
      el.textContent = "no change";
      el.className = "ov-hero-stat-trend flat";
      return;
    }
    const pct = Math.round((diff / Math.abs(previous)) * 100);
    const isGood = invert ? diff < 0 : diff > 0;
    el.textContent = (diff > 0 ? "▲" : "▼") + " "
      + Math.abs(pct) + "% vs yesterday";
    el.className = "ov-hero-stat-trend " + (isGood ? "up" : "down");
  },

  renderChart(daily) {
    const canvas = $("#chart-daily");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (this.chart) this.chart.destroy();
    this.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: daily.map(d => d.date),
        datasets: [
          {
            label: "Searches",
            data: daily.map(d => d.searches),
            borderColor: "#34d399",
            backgroundColor: "rgba(52, 211, 153, 0.12)",
            fill: true, tension: 0.4, borderWidth: 2,
            pointRadius: 3, pointBackgroundColor: "#34d399",
          },
          {
            label: "Captchas",
            data: daily.map(d => d.captchas),
            borderColor: "#fbbf24",
            backgroundColor: "rgba(251, 191, 36, 0.12)",
            fill: true, tension: 0.4, borderWidth: 2,
            pointRadius: 3, pointBackgroundColor: "#fbbf24",
          },
          {
            label: "Empty",
            data: daily.map(d => d.empty),
            borderColor: "#6b7280",
            backgroundColor: "rgba(107, 114, 128, 0.08)",
            fill: true, tension: 0.4, borderWidth: 1,
            borderDash: [4, 4],
            pointRadius: 2, pointBackgroundColor: "#6b7280",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: { color: "#cbd5e1", usePointStyle: true,
                      boxWidth: 8, boxHeight: 8 },
          },
          tooltip: {
            backgroundColor: "rgba(15, 20, 25, 0.95)",
            borderColor: "rgba(99, 102, 241, 0.3)",
            borderWidth: 1,
          },
        },
        scales: {
          x: { ticks: { color: "#8a93a6" },
               grid: { color: "rgba(255, 255, 255, 0.04)" } },
          y: { ticks: { color: "#8a93a6" },
               grid: { color: "rgba(255, 255, 255, 0.04)" },
               beginAtZero: true },
        },
      },
    });
  },

  // ── Recent activity feed ─────────────────────────────────────

  async loadRecentActivity() {
    try {
      const runs = await api("/api/runs?limit=8");
      const el = $("#ov-activity");
      if (!runs || !runs.length) {
        el.innerHTML = `<div class="empty-state" style="padding: 20px 0;">
          No runs yet. Start your first one!
        </div>`;
        return;
      }
      el.innerHTML = runs.slice(0, 8).map(r => this._renderActivityItem(r)).join("");
    } catch (e) {
      console.error("recent activity:", e);
    }
  },

  _renderActivityItem(run) {
    let dotClass = "neutral";
    let title;
    if (run.exit_code == null) {
      dotClass = "neutral";
      title = `Running… #${run.id}`;
    } else if (run.exit_code === 0) {
      dotClass = "ok";
      title = `Run #${run.id} completed`;
    } else {
      dotClass = "err";
      title = `Run #${run.id} failed`;
    }
    const when   = formatAgo(run.started_at);
    const ads    = run.total_ads ?? 0;
    const caps   = run.captchas ?? 0;
    const prof   = run.profile_name || "—";
    return `
      <div class="ov-activity-item">
        <div class="ov-activity-dot ${dotClass}"></div>
        <div class="ov-activity-body">
          <div class="ov-activity-title">${escapeHtml(title)}</div>
          <div class="ov-activity-meta">
            ${escapeHtml(prof)} · ${ads} ad(s)${caps > 0 ? ` · ${caps} captcha(s)` : ""} · ${when}
          </div>
        </div>
      </div>
    `;
  },

  // ── Top competitors ──────────────────────────────────────────

  async loadTopCompetitors() {
    try {
      const comps = await api("/api/competitors");
      const tbody = $("#top-competitors-tbody");
      const badge = $("#top-competitors-badge");
      const list  = Array.isArray(comps) ? comps
                  : (comps.items || comps.list || []);
      if (badge) badge.textContent = list.length;

      if (!list.length) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No competitors tracked yet</td></tr>`;
        return;
      }

      // Top 10 by frequency
      const top = list
        .sort((a, b) => (b.count || b.queries_count || 0) - (a.count || a.queries_count || 0))
        .slice(0, 10);

      tbody.innerHTML = top.map(c => `
        <tr>
          <td><strong>${escapeHtml(c.domain || "—")}</strong></td>
          <td class="muted">${escapeHtml((c.title || c.sample_title || "—").slice(0, 60))}</td>
          <td>${c.count ?? c.queries_count ?? "—"}</td>
          <td class="muted">${escapeHtml(formatAgo(c.last_seen))}</td>
        </tr>
      `).join("");
    } catch (e) {
      console.error("competitors:", e);
    }
  },

  // ── Per-profile health rollup ────────────────────────────────

  async loadProfileHealth() {
    try {
      const profiles = await api("/api/profiles");
      const tbody = $("#profile-health-tbody");
      if (!profiles || !profiles.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No profiles yet</td></tr>`;
        return;
      }

      tbody.innerHTML = profiles.map(p => {
        const passed = p.selfcheck_passed ?? "—";
        const total  = p.selfcheck_total  ?? "—";
        const scText = (passed != null && total != null)
                        ? `${passed}/${total}` : "—";
        const scClass = (passed != null && total != null)
                        ? (passed === total ? "pill pill-ok"
                         : passed > total * 0.8 ? "pill pill-warn"
                         : "pill pill-err")
                        : "pill";

        const captchas  = p.captchas_24h ?? 0;
        const searches  = p.searches_24h ?? 0;
        const rate      = (searches + captchas) > 0
                          ? (captchas / (searches + captchas))
                          : 0;
        const rateStr   = (rate * 100).toFixed(1) + "%";
        const rateClass = rate > 0.2 ? "pill pill-err"
                        : rate > 0.05 ? "pill pill-warn"
                        : "pill pill-ok";

        const status  = p.status || "ready";
        return `
          <tr style="cursor: pointer;" onclick="navigate('profile')">
            <td><strong>${escapeHtml(p.name || "—")}</strong></td>
            <td class="muted">${escapeHtml(p.template || "—")}</td>
            <td><span class="${scClass}">${scText}</span></td>
            <td>${searches}</td>
            <td><span class="${rateClass}">${rateStr}</span></td>
            <td><span class="pill pill-${status}">${status}</span></td>
          </tr>
        `;
      }).join("");
    } catch (e) {
      console.error("profile health:", e);
    }
  },
};

// Simple "time ago" helper (e.g. "3 min ago", "2 days ago")
function formatAgo(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = Math.round((Date.now() - d.getTime()) / 1000);
  if (isNaN(diff)) return "—";
  if (diff < 60)    return `${diff}s ago`;
  if (diff < 3600)  return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}
