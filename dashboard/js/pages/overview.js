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
    ]);

    // Auto-refresh — quick polls, only the cheap stuff
    clearInterval(this._pollTimer);
    this._pollTimer = setInterval(() => {
      if (currentPage !== "overview") { clearInterval(this._pollTimer); return; }
      this.loadActiveRuns();
      this.loadVaultTile();
      this.loadSchedulerTile();
      this.loadCaptchaTile();
    }, 5000);

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
    if (typeof this._unsubRunFinished === "function") this._unsubRunFinished();
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
