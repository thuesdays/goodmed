// ═══════════════════════════════════════════════════════════════
// pages/overview.js — operator-focused dashboard home.
//
// Five hero tiles (vault / scheduler / active runs / proxies / captchas)
// + quick actions + live runs strip + profile health table + recent
// activity feed + fingerprint health.
//
// Competitor / search-volume metrics moved to the Competitors page;
// this file no longer owns the 7-day chart or top-domains table.
// ═══════════════════════════════════════════════════════════════

const Overview = {
  _pollTimer: null,
  _healthPollTimer: null,
  _unsubRunFinished: null,

  async init() {
    await Promise.all([
      this.loadVaultTile(),
      this.loadSchedulerTile(),
      this.loadActiveRuns(),
      this.loadProxyTile(),
      this.loadCaptchaTile(),
      this.loadProfileHealth(),
      this.loadActivityFeed(),
      this.loadFingerprintHealth(),
      this.loadAdDensity(),
      this.loadHealthBanner(),
    ]);
    // Wire the refresh button on the density panel (idempotent)
    const refreshBtn = document.getElementById("ov-density-refresh");
    if (refreshBtn && refreshBtn.dataset._wired !== "1") {
      refreshBtn.dataset._wired = "1";
      refreshBtn.addEventListener("click", () => this.loadAdDensity());
    }
    // Wire the Re-check button on the health banner (idempotent)
    const recheckBtn = document.getElementById("ov-health-recheck");
    if (recheckBtn && recheckBtn.dataset._wired !== "1") {
      recheckBtn.dataset._wired = "1";
      recheckBtn.addEventListener("click", () => this.loadHealthBanner({force: true}));
    }

    // Auto-refresh — quick polls, only the cheap stuff
    clearInterval(this._pollTimer);
    this._pollTimer = setInterval(() => {
      if (currentPage !== "overview") { clearInterval(this._pollTimer); return; }
      this.loadActiveRuns();
      this.loadVaultTile();
      this.loadSchedulerTile();
      this.loadCaptchaTile();
    }, 5000);

    // Health banner re-poll — RC-23 fix. Backend caches verdict for
    // 60s and individual binary probes for 1h with mtime-aware
    // refresh, so post-deploy version changes are detected
    // automatically once the cache expires. We re-poll every 5min
    // with refresh=1 every 4th call (every 20min) to bypass the
    // backend cache and pick up Chrome rebuilds even when mtime
    // tracking misses.
    clearInterval(this._healthPollTimer);
    this._healthPollCount = 0;
    this._healthPollTimer = setInterval(() => {
      if (currentPage !== "overview") {
        clearInterval(this._healthPollTimer); return;
      }
      this._healthPollCount += 1;
      const force = (this._healthPollCount % 4 === 0);
      this.loadHealthBanner({force, silent: true});
    }, 5 * 60 * 1000);  // 5 minutes

    // SSE invalidate on run finish — refresh the heavier pieces
    if (typeof onSystemEvent === "function") {
      this._unsubRunFinished = onSystemEvent("run_finished", () => {
        this.loadProfileHealth();
        this.loadActivityFeed();
        this.loadCaptchaTile();
        this.loadActiveRuns();
      });
    }
  },

  teardown() {
    clearInterval(this._pollTimer);
    clearInterval(this._healthPollTimer);
    if (typeof this._unsubRunFinished === "function") this._unsubRunFinished();
  },

  // ── Health banner — Chrome / chromedriver version compatibility ──
  // Renders a top-of-page alert when /api/health/versions reports a
  // mismatch or missing binary. Cheap one-shot fetch on init; the
  // backend caches the verdict for 60s. Click "Re-check" to bypass
  // the cache (e.g. after deploying a new chromium build).
  async loadHealthBanner(opts = {}) {
    const banner  = document.getElementById("ov-health-banner");
    const icon    = document.getElementById("ov-health-icon");
    const titleEl = document.getElementById("ov-health-title");
    const detail  = document.getElementById("ov-health-detail");
    const recheckBtn = document.getElementById("ov-health-recheck");
    if (!banner || !titleEl || !detail) return;

    // Background poll mode (silent: true) — skip the "Probing…" button
    // feedback. The banner still updates if the verdict changes.
    if (opts.silent) {
      try { await this._renderHealthVerdict(!!opts.force); }
      catch (e) { /* swallow — silent path */ }
      return;
    }

    if (opts.force && recheckBtn) {
      recheckBtn.disabled = true;
      const orig = recheckBtn.textContent;
      recheckBtn.textContent = "Probing…";
      try { await this._renderHealthVerdict(true); }
      finally {
        recheckBtn.disabled = false;
        recheckBtn.textContent = orig;
      }
    } else {
      await this._renderHealthVerdict(false);
    }
  },

  async _renderHealthVerdict(force) {
    const banner  = document.getElementById("ov-health-banner");
    const icon    = document.getElementById("ov-health-icon");
    const titleEl = document.getElementById("ov-health-title");
    const detail  = document.getElementById("ov-health-detail");
    let verdict;
    try {
      verdict = await api(
        "/api/health/versions" + (force ? "?refresh=1" : "")
      );
    } catch (e) {
      // Endpoint failure — stay silent rather than spam the user
      // with an "endpoint broke" banner. Logged for ops.
      console.warn("[overview] health/versions fetch failed:", e);
      banner.style.display = "none";
      return;
    }

    // Reset modifier classes
    banner.classList.remove("is-critical", "is-warn");

    if (verdict && verdict.ok && verdict.level === "ok") {
      // All good — banner hidden. (Could add a transient "✓ all
      // versions match" toast on Re-check click, but quiet success
      // is the right default for a startup banner.)
      banner.style.display = "none";
      return;
    }

    const level  = verdict?.level || "warn";
    const reason = verdict?.reason || "Compatibility check returned no detail.";
    let title = "Environment problem";
    if (level === "critical") {
      title = "Chrome / ChromeDriver version mismatch — sessions will fail";
      banner.classList.add("is-critical");
      if (icon) icon.textContent = "⛔";
    } else {
      title = "Environment warning";
      banner.classList.add("is-warn");
      if (icon) icon.textContent = "⚠";
    }

    // Add version pair to the detail when available, so the user can
    // see at a glance what's wrong without parsing the long reason.
    let detailText = reason;
    if (verdict?.chrome_version || verdict?.driver_version) {
      detailText = `Chrome ${verdict.chrome_version || "?"} ↔ `
                 + `ChromeDriver ${verdict.driver_version || "?"}.\n`
                 + reason;
    }

    titleEl.textContent = title;
    detail.textContent  = detailText;
    banner.style.display = "";
  },

  // ─── Hero tiles ────────────────────────────────────────────

  async loadVaultTile() {
    const stateEl = $("#ov-vault-state");
    const subEl   = $("#ov-vault-sub");
    try {
      const v = await api("/api/vault/status");
      if (!v.initialized) {
        stateEl.textContent = "Not set";
        subEl.textContent   = "click to initialize";
        return;
      }
      if (v.unlocked) {
        stateEl.textContent = "🔓 Unlocked";
        // Pull item count for a useful sub-line
        try {
          const items = await api("/api/vault/items");
          const total = (items.items || []).length;
          subEl.textContent = `${total} item${total === 1 ? "" : "s"} in vault`;
        } catch { subEl.textContent = "ready"; }
      } else {
        stateEl.textContent = "🔒 Locked";
        subEl.textContent   = "click to unlock";
      }
    } catch (e) {
      stateEl.textContent = "—";
      subEl.textContent   = "vault unavailable";
    }
  },

  async loadSchedulerTile() {
    const stateEl = $("#ov-sched-state");
    const subEl   = $("#ov-sched-sub");
    try {
      const s = await api("/api/scheduler/status");
      if (s.is_running && (s.health || "ok") === "ok") {
        stateEl.textContent = "Running";
        if (s.next_run_at) {
          const d = new Date(s.next_run_at);
          subEl.textContent = `next: ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
        } else {
          subEl.textContent = `${s.runs_today ?? 0}/${s.target_runs_per_day ?? "—"} today`;
        }
      } else if (s.is_running) {
        stateEl.textContent = "Wedged";
        subEl.textContent   = "click to recover";
      } else {
        stateEl.textContent = "Stopped";
        subEl.textContent   = "click to start";
      }
    } catch (e) {
      stateEl.textContent = "—";
      subEl.textContent   = "—";
    }
  },

  async loadActiveRuns() {
    const countEl = $("#ov-active-count");
    const subEl   = $("#ov-active-sub");
    const panel   = $("#ov-active-panel");
    const list    = $("#ov-active-runs");
    try {
      const data = await api("/api/runs/active").catch(() => ({ active: [] }));
      const active = data.active || data || [];
      const n = Array.isArray(active) ? active.length : 0;
      countEl.textContent = n;
      subEl.textContent   = n ? `live · click to monitor` : "everything quiet";

      if (n > 0) {
        panel.style.display = "block";
        list.innerHTML = active.map(r => {
          const profile = r.profile_name || r.profile || "—";
          const queries = r.queries_done != null
            ? `${r.queries_done}/${r.queries_total ?? "?"} queries`
            : "starting…";
          const dur = r.started_at
            ? timeAgo(r.started_at)
            : "—";
          return `
            <div class="ov-run-card">
              <div class="ov-run-card-name">
                <span class="ov-run-card-pulse"></span>
                ${escapeHtml(profile)}
              </div>
              <div class="ov-run-card-meta">${escapeHtml(dur)} · run #${r.id ?? "?"}</div>
              <div class="ov-run-card-progress">${escapeHtml(queries)}</div>
            </div>`;
        }).join("");
      } else {
        panel.style.display = "none";
      }
    } catch (e) {
      countEl.textContent = "—";
      subEl.textContent   = "—";
      panel.style.display = "none";
    }
  },

  async loadProxyTile() {
    const countEl = $("#ov-proxy-count");
    const subEl   = $("#ov-proxy-sub");
    try {
      const data = await api("/api/proxies");
      const list = Array.isArray(data) ? data : (data.proxies || []);
      const total = list.length;
      const ok    = list.filter(p => p.last_status === "ok").length;
      const err   = list.filter(p => p.last_status === "error").length;
      countEl.textContent = `${ok}/${total}`;
      const parts = [];
      if (err) parts.push(`${err} error`);
      const untested = total - ok - err;
      if (untested) parts.push(`${untested} untested`);
      subEl.textContent = parts.length ? parts.join(" · ") : "all healthy";
    } catch (e) {
      countEl.textContent = "—";
      subEl.textContent   = "—";
    }
  },

  async loadCaptchaTile() {
    const countEl = $("#ov-captcha-count");
    const subEl   = $("#ov-captcha-sub");
    try {
      const stats = await api("/api/stats");
      const c = stats?.captchas_24h ?? stats?.captchas ?? 0;
      const r = stats?.searches_24h ?? 0;
      countEl.textContent = c;
      const rate = (c + r) > 0 ? (100 * c / (c + r)).toFixed(1) : "0.0";
      subEl.textContent = c
        ? `${rate}% rate · ${r} searches`
        : "clean — no captchas in 24h";
    } catch (e) {
      countEl.textContent = "—";
      subEl.textContent   = "—";
    }
  },

  // ─── Ad density trend widget ───────────────────────────────
  // The "is our algo improving things" panel. Headline numbers +
  // 14-day sparkline + per-profile/per-IP drill-downs. Single
  // backend call to /api/metrics/ad-density returns everything.
  async loadAdDensity() {
    const body = document.getElementById("ov-density-body");
    if (!body) return;
    try {
      const m = await api("/api/metrics/ad-density");
      body.innerHTML = this._renderDensityWidget(m);
    } catch (e) {
      body.innerHTML =
        `<div class="muted" style="padding:12px;">
           Failed to load metrics: ${escapeHtml(e.message || e)}
         </div>`;
    }
  },

  _renderDensityWidget(m) {
    const s = m.summary || {};
    const daily = m.daily || [];
    const profiles = m.per_profile || [];
    const ips = m.per_ip || [];

    // Headline: avg ads/query 7d + delta + run count + ctr
    const avg7  = (s.avg_ads_per_query_7d ?? 0).toFixed(2);
    const avg24 = (s.avg_ads_per_query_24h ?? 0).toFixed(2);
    const delta = s.delta_pct;
    const deltaStr = delta == null
      ? `<span class="muted">first week — no comparison yet</span>`
      : (delta >= 0
         ? `<span style="color:var(--healthy,#22c55e)">▲ ${delta.toFixed(1)}%</span> vs prior 7d`
         : `<span style="color:var(--critical,#ef4444)">▼ ${Math.abs(delta).toFixed(1)}%</span> vs prior 7d`);
    const runs7 = s.total_runs_7d ?? 0;
    const ads7  = s.total_ads_7d ?? 0;
    const q7    = s.total_queries_7d ?? 0;
    const clicks7 = s.total_clicks_7d ?? 0;
    const ctr   = ((s.ctr_7d ?? 0) * 100).toFixed(1);

    // Sparkline: ads_per_query for last 14 days, simple SVG
    const spark = this._renderSparkline(
      daily.map(d => d.ads_per_query || 0),
      daily.map(d => d.date),
    );

    // Per-profile mini table
    const profileRows = profiles.length
      ? profiles.map(p => `
          <tr>
            <td>${escapeHtml(p.profile_name)}</td>
            <td class="num">${p.runs}</td>
            <td class="num"><strong>${p.ads_per_query.toFixed(2)}</strong></td>
          </tr>`).join("")
      : `<tr><td colspan="3" class="dense-empty-cell">No completed runs in last 7 days</td></tr>`;

    // Per-IP mini table
    const ipRows = ips.length
      ? ips.map(p => `
          <tr>
            <td><code style="font-size:11px;">${escapeHtml(p.ip || "?")}</code>
                ${p.country ? `<span class="muted" style="font-size:11px;"> · ${escapeHtml(p.country)}</span>` : ""}</td>
            <td class="num">${p.runs}</td>
            <td class="num"><strong>${p.ads_per_query.toFixed(2)}</strong></td>
          </tr>`).join("")
      : `<tr><td colspan="3" class="dense-empty-cell">No IP data yet (runs without geo lookup)</td></tr>`;

    return `
      <div style="display: grid;
                  grid-template-columns: minmax(260px, 1fr) minmax(280px, 2fr);
                  gap: 16px; padding: 12px;">
        <!-- Left: headline numbers -->
        <div>
          <div style="display:flex; align-items:baseline; gap:8px;">
            <div style="font-size: 32px; font-weight: 600;
                        color: var(--accent, #6366f1);">
              ${avg7}
            </div>
            <div class="muted" style="font-size: 12px;">ads / query · 7d</div>
          </div>
          <div style="font-size: 12px; margin-top: 4px;">${deltaStr}</div>

          <div style="display: grid; grid-template-columns: 1fr 1fr;
                      gap: 8px; margin-top: 14px; font-size: 12px;">
            <div>
              <div class="muted">Last 24h avg</div>
              <div style="font-size: 16px; font-weight: 500;">${avg24}</div>
            </div>
            <div>
              <div class="muted">Runs 7d</div>
              <div style="font-size: 16px; font-weight: 500;">${runs7}</div>
            </div>
            <div>
              <div class="muted">Total ads 7d</div>
              <div style="font-size: 16px; font-weight: 500;">${ads7}</div>
            </div>
            <div>
              <div class="muted">CTR proxy</div>
              <div style="font-size: 16px; font-weight: 500;">${ctr}%
                <span class="muted" style="font-size:10px;">(${clicks7}/${ads7})</span>
              </div>
            </div>
          </div>

          <div style="margin-top: 14px;">
            <div class="muted" style="font-size: 10px; text-transform: uppercase;
                                       margin-bottom: 4px;">
              14-day trend
            </div>
            ${spark}
          </div>
        </div>

        <!-- Right: drill-down tables -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
          <div>
            <div class="muted" style="font-size: 10px; text-transform: uppercase;
                                       margin-bottom: 6px;">
              Top profiles · 7d
            </div>
            <table class="dense-table" style="font-size: 12px;">
              <thead><tr>
                <th>Profile</th><th class="num">Runs</th><th class="num">Ads/q</th>
              </tr></thead>
              <tbody>${profileRows}</tbody>
            </table>
          </div>
          <div>
            <div class="muted" style="font-size: 10px; text-transform: uppercase;
                                       margin-bottom: 6px;">
              Top IPs · 7d
            </div>
            <table class="dense-table" style="font-size: 12px;">
              <thead><tr>
                <th>IP</th><th class="num">Runs</th><th class="num">Ads/q</th>
              </tr></thead>
              <tbody>${ipRows}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  },

  _renderSparkline(values, labels) {
    if (!values || !values.length) {
      return `<div class="muted" style="font-size: 11px;">No data yet</div>`;
    }
    const W = 240, H = 50, P = 4;
    const max = Math.max(...values, 0.5);  // avoid /0
    const stepX = values.length > 1 ? (W - 2 * P) / (values.length - 1) : 0;
    const points = values.map((v, i) => {
      const x = P + i * stepX;
      const y = H - P - ((v / max) * (H - 2 * P));
      return [x, y];
    });
    const path = points.map((p, i) =>
      (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)
    ).join(" ");
    // Gradient fill underneath the line
    const fill = `M${points[0][0]},${H - P} ` +
                 points.map(p => `L${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ") +
                 ` L${points[points.length - 1][0]},${H - P} Z`;

    const tooltips = values.map((v, i) =>
      `${labels[i] || ""}: ${v.toFixed(2)} ads/q`
    ).join(" · ");

    return `
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}"
           preserveAspectRatio="none"
           style="display: block; width: 100%;">
        <title>${escapeHtml(tooltips)}</title>
        <defs>
          <linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--accent, #6366f1)" stop-opacity=".3"/>
            <stop offset="100%" stop-color="var(--accent, #6366f1)" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path d="${fill}" fill="url(#sparkfill)" stroke="none"/>
        <path d="${path}" fill="none"
              stroke="var(--accent, #6366f1)" stroke-width="1.8"/>
        ${points.map(p => `
          <circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}"
                  r="2" fill="var(--accent, #6366f1)"/>
        `).join("")}
      </svg>
    `;
  },

  // ─── Profile health (left column) ──────────────────────────

  async loadProfileHealth() {
    const tbody = $("#profile-health-tbody");
    try {
      const profiles = await api("/api/profiles");
      if (!profiles || !profiles.length) {
        tbody.innerHTML =
          `<tr><td colspan="6" class="dense-empty-cell">No profiles yet — create one on the Profiles page.</td></tr>`;
        return;
      }
      // Pull FP scores in parallel — saves one round-trip per row
      let fpByName = {};
      try {
        const summary = await api("/api/fingerprints/summary");
        for (const r of (summary.profiles || [])) fpByName[r.profile_name] = r;
      } catch {}

      tbody.innerHTML = profiles.map(p => {
        const status   = (p.status || "idle").toLowerCase();
        const blocks   = p.consecutive_blocks || 0;
        const captchas = p.captchas_24h ?? 0;
        const searches = p.searches_24h ?? 0;
        const rate     = (searches + captchas) > 0
                         ? (100 * captchas / (searches + captchas)).toFixed(1) + "%"
                         : "—";
        const rateCls  = (searches + captchas) > 0
                         ? (captchas / (searches + captchas) > 0.2 ? "pill-err"
                          : captchas / (searches + captchas) > 0.05 ? "pill-warn"
                          : "pill-ok")
                         : "pill-idle";
        const fp     = fpByName[p.name] || {};
        const score  = fp.coherence_score;
        const fpCls  = score == null ? "pill-idle"
                     : score >= 90   ? "pill-ok"
                     : score >= 75   ? ""
                     : "pill-err";
        const fpHtml = score == null
          ? '<span class="pill pill-idle">—</span>'
          : `<span class="pill ${fpCls}">${score}</span>`;
        const lastRun = p.last_run_at ? timeAgo(p.last_run_at) : "—";
        const rowCls = `profile-status-${status === "running" ? "running" :
                                          blocks >= 3        ? "blocked" :
                                          status === "active" ? "active"  : "idle"}`;
        return `
          <tr class="${rowCls}" style="cursor: pointer;"
              onclick="(function(){configCache.browser=configCache.browser||{};configCache.browser.profile_name='${escapeHtml(p.name)}';navigate('profile');})()">
            <td><strong>${escapeHtml(p.name)}</strong>
              ${blocks ? `<span class="pill pill-warn" style="margin-left:6px;">⚠ ${blocks} blocks</span>` : ""}
            </td>
            <td><span class="pill pill-${status}">${escapeHtml(status)}</span></td>
            <td>${fpHtml}</td>
            <td class="num">${searches}</td>
            <td class="num"><span class="pill ${rateCls}">${rate}</span></td>
            <td class="muted" style="font-family: ui-monospace, monospace; font-size: 11px;">
              ${escapeHtml(lastRun)}
            </td>
          </tr>`;
      }).join("");
    } catch (e) {
      console.error("profile health:", e);
      tbody.innerHTML = `<tr><td colspan="6" class="dense-empty-cell">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
    }
  },

  // ─── Activity feed (right column) ──────────────────────────

  async loadActivityFeed() {
    const host = $("#ov-activity");
    if (!host) return;
    try {
      // Mix recent runs + recent warmups + recent fingerprint events
      // into a single timeline. /api/runs gives us the bulk of it.
      const runs = await api("/api/runs?limit=15").catch(() => []);
      const items = (Array.isArray(runs) ? runs : runs.runs || []).map(r => ({
        kind:  r.exit_code === 0 ? "run-ok" : "run-fail",
        icon:  r.exit_code === 0 ? "✓" : "✗",
        title: r.exit_code === 0
                 ? `Run #${r.id} ok — ${r.profile_name}`
                 : `Run #${r.id} failed — ${r.profile_name}`,
        meta:  `${r.total_queries ?? 0} queries · ${r.captchas ?? 0} captchas`,
        at:    r.finished_at || r.started_at,
      }));

      if (!items.length) {
        host.innerHTML = '<div class="dense-empty" style="padding: 24px 18px;">No activity yet — start your first run.</div>';
        return;
      }

      // Take 12 most-recent
      items.sort((a, b) => (a.at < b.at ? 1 : -1));
      host.innerHTML = items.slice(0, 12).map(it => `
        <div class="ov-feed-item">
          <div class="ov-feed-icon ${it.kind}">${it.icon}</div>
          <div>
            <div class="ov-feed-body">${escapeHtml(it.title)}</div>
            <div class="ov-feed-meta">${escapeHtml(it.meta || "")}</div>
          </div>
          <div class="ov-feed-when">${escapeHtml(it.at ? timeAgo(it.at) : "—")}</div>
        </div>
      `).join("");
    } catch (e) {
      host.innerHTML = `<div class="dense-empty" style="padding: 24px 18px;">Feed unavailable: ${escapeHtml(e.message)}</div>`;
    }
  },

  // ─── Fingerprint health rollup ─────────────────────────────

  async loadFingerprintHealth() {
    try {
      const resp = await api("/api/fingerprints/summary");
      const rows = resp.profiles || [];
      const tbody = $("#overview-fp-tbody");

      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="dense-empty-cell">No profiles yet</td></tr>`;
        $("#ov-fp-avg-score").textContent     = "—";
        $("#ov-fp-warn-count").textContent    = "—";
        $("#ov-fp-missing-count").textContent = "—";
        return;
      }

      const withFp  = rows.filter(r => r.coherence_score != null);
      const missing = rows.length - withFp.length;
      const warn    = withFp.filter(r => r.coherence_score < 75).length;
      const avg     = withFp.length
                      ? Math.round(withFp.reduce((s, r) => s + r.coherence_score, 0) / withFp.length)
                      : null;

      $("#ov-fp-avg-score").textContent     = avg == null ? "—" : avg;
      $("#ov-fp-warn-count").textContent    = warn;
      $("#ov-fp-missing-count").textContent = missing;

      const tileCls = (n) => n > 0 ? "warn" : "ok";
      $("#ov-fp-tile-avg").className     = "overview-fp-summary-tile " +
        (avg == null ? "" : avg >= 90 ? "ok" : avg >= 75 ? "" : avg >= 55 ? "warn" : "bad");
      $("#ov-fp-tile-warn").className    = "overview-fp-summary-tile " + tileCls(warn);
      $("#ov-fp-tile-missing").className = "overview-fp-summary-tile " + (missing ? "bad" : "ok");

      // Sort: missing first, then by score asc
      rows.sort((a, b) => {
        const aMiss = a.coherence_score == null;
        const bMiss = b.coherence_score == null;
        if (aMiss !== bMiss) return aMiss ? -1 : 1;
        return (a.coherence_score ?? 999) - (b.coherence_score ?? 999);
      });

      tbody.innerHTML = rows.map(r => {
        const score   = r.coherence_score;
        const hasFp   = score != null;
        const rowCls  = !hasFp ? "fp-row-bad"
                       : score < 55 ? "fp-row-bad"
                       : score < 75 ? "fp-row-warn" : "";
        const scoreCls = !hasFp ? "bad" : score >= 90 ? "ok" : score >= 75 ? "" : score >= 55 ? "warn" : "bad";
        const tmpl = r.template_name || r.template_id || (hasFp ? "unknown" : "no fingerprint");
        return `
          <tr class="${rowCls}" data-profile="${escapeHtml(r.profile_name)}" style="cursor: pointer;">
            <td><strong>${escapeHtml(r.profile_name)}</strong></td>
            <td class="muted">${escapeHtml(tmpl)}</td>
            <td class="col-score num ${scoreCls}">${hasFp ? score : "—"}</td>
            <td class="col-snaps num">${r.history_count ?? 0}</td>
            <td class="col-when muted">${r.current_ts ? timeAgo(r.current_ts) : "—"}</td>
          </tr>`;
      }).join("");

      tbody.querySelectorAll("tr[data-profile]").forEach(tr =>
        tr.addEventListener("click", () => {
          location.hash = `#fingerprint?profile=${encodeURIComponent(tr.dataset.profile)}`;
          navigate("fingerprint");
        })
      );
    } catch (e) {
      console.error("fingerprint health:", e);
      const tbody = $("#overview-fp-tbody");
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="dense-empty-cell">Failed: ${escapeHtml(e.message)}</td></tr>`;
    }
  },
};
