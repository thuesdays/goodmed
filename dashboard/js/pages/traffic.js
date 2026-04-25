// ═══════════════════════════════════════════════════════════════
// pages/traffic.js — bandwidth analytics dashboard
//
// Consumes 4 API endpoints:
//   /api/traffic/summary    — totals + timeseries for chart
//   /api/traffic/by-profile — per-profile breakdown table
//   /api/traffic/by-domain  — top N domains (filterable by profile)
//
// All four are reloaded whenever the user changes the time range or
// the profile filter. Cheap — the underlying SQL is already indexed
// on (profile, hour_bucket) so even 90-day queries return in <50ms.
// ═══════════════════════════════════════════════════════════════

const Traffic = {
  _chart:        null,
  _refreshTimer: null,
  _profileFilter: "",   // empty = all profiles

  async init() {
    $("#traffic-refresh-btn").addEventListener("click", () => this.loadAll());
    $("#traffic-range").addEventListener("change", () => this.loadAll());
    $("#tr-domain-profile-filter").addEventListener("change", (e) => {
      this._profileFilter = e.target.value || "";
      this.loadDomains();
    });

    await this.loadAll();

    // Auto-refresh every 30s while this page is open. Cheap because
    // the queries hit only the hot end of an indexed table.
    clearInterval(this._refreshTimer);
    this._refreshTimer = setInterval(() => {
      if (currentPage === "traffic") {
        this.loadAll({ silent: true });
      } else {
        clearInterval(this._refreshTimer);
      }
    }, 30_000);
  },

  teardown() {
    clearInterval(this._refreshTimer);
    this._refreshTimer = null;
    if (this._chart) {
      this._chart.destroy();
      this._chart = null;
    }
  },

  _getHours() {
    return parseInt($("#traffic-range").value, 10) || 24;
  },

  /** Decide hour-vs-day chart granularity based on range. We use hour
   *  buckets for ranges up to 48h (gives 24-48 points — dense but readable),
   *  day buckets for longer ranges (7d = 7 points, 90d = 90 points — too
   *  dense as hours, perfect as days). */
  _getBucket() {
    return this._getHours() <= 48 ? "hour" : "day";
  },

  async loadAll(opts = {}) {
    if (!opts.silent) {
      $("#tr-total-bytes").textContent = "…";
    }
    await Promise.all([
      this.loadSummary(),
      this.loadProfiles(),
      this.loadDomains(),
    ]);
  },

  async loadSummary() {
    const hours  = this._getHours();
    const bucket = this._getBucket();
    try {
      const s = await api(`/api/traffic/summary?hours=${hours}&bucket=${bucket}`);
      $("#tr-total-bytes").textContent   = formatBytes(s.total_bytes || 0);
      $("#tr-total-requests").textContent = (s.total_requests || 0).toLocaleString();
      $("#tr-profile-count").textContent  = String(s.profile_count || 0);
      $("#tr-domain-count").textContent   = String(s.domain_count  || 0);

      // Avg bytes per request — useful "is my traffic chunky or chatty"
      const avgPerReq = (s.total_requests && s.total_bytes)
        ? s.total_bytes / s.total_requests
        : 0;
      $("#tr-avg-per-req").textContent =
        `avg ${formatBytes(avgPerReq, 1)}/req`;

      $("#tr-total-bytes-sub").textContent =
        this._describeRange(hours);

      $("#tr-chart-bucket-hint").textContent =
        bucket === "hour" ? "hour buckets" : "daily totals";

      this._renderChart(s.timeseries || [], bucket);
    } catch (e) {
      console.error("Traffic summary load:", e);
      $("#tr-total-bytes").textContent = "err";
    }
  },

  async loadProfiles() {
    const hours = this._getHours();
    try {
      const resp = await api(`/api/traffic/by-profile?hours=${hours}`);
      this._renderProfileTable(resp.profiles || []);
      // Also populate the filter dropdown on the domains card
      this._renderProfileFilter(resp.profiles || []);
    } catch (e) {
      console.error("Traffic profiles load:", e);
    }
  },

  async loadDomains() {
    const hours = this._getHours();
    const qs = new URLSearchParams({ hours, limit: 50 });
    if (this._profileFilter) qs.set("profile", this._profileFilter);
    try {
      const resp = await api(`/api/traffic/by-domain?${qs}`);
      this._renderDomainTable(resp.domains || [], this._profileFilter);
    } catch (e) {
      console.error("Traffic domains load:", e);
    }
  },

  // ─── Renderers ────────────────────────────────────────────────

  _renderChart(series, bucket) {
    const canvas = document.getElementById("tr-chart");
    if (!canvas) return;
    const wrap = canvas.closest(".chart-wrap") || canvas.parentElement;
    let emptyEl = wrap?.querySelector(".chart-empty-state");

    // Empty-state handling — if series is empty OR every bucket has
    // both 0 bytes and 0 requests, show a helpful hint instead of an
    // empty chart. A blank chart isn't useful and looks broken; a
    // message that says "no traffic yet" tells the user to either run
    // something or wait for the collector to flush.
    const totalBytes = series.reduce((a, r) => a + (r.bytes    || 0), 0);
    const totalReqs  = series.reduce((a, r) => a + (r.requests || 0), 0);
    const isEmpty = series.length === 0 || (totalBytes === 0 && totalReqs === 0);

    if (isEmpty) {
      if (this._chart) { this._chart.destroy(); this._chart = null; }
      canvas.style.display = "none";
      if (!emptyEl && wrap) {
        emptyEl = document.createElement("div");
        emptyEl.className = "chart-empty-state";
        emptyEl.innerHTML = `
          <div class="chart-empty-icon">📊</div>
          <div class="chart-empty-title">No traffic data for this range</div>
          <div class="chart-empty-hint">
            Traffic is collected during runs and flushed every 30s.
            Start a run, or expand the range if nothing's run recently.
          </div>
        `;
        wrap.appendChild(emptyEl);
      }
      if (emptyEl) emptyEl.style.display = "";
      return;
    }

    // Real data — hide the empty state if previously shown
    if (emptyEl) emptyEl.style.display = "none";
    canvas.style.display = "";

    const ctx = canvas.getContext("2d");

    // Format labels for readability — "15" (hour) or "04-22" (day)
    const labels = series.map(r => {
      if (bucket === "hour") {
        // 'YYYY-MM-DD HH' → 'HH:00'
        const parts = (r.time || "").split(" ");
        return parts.length > 1 ? `${parts[1]}:00` : r.time;
      }
      // 'YYYY-MM-DD' → 'MM-DD'
      return (r.time || "").slice(5);
    });

    const bytesData = series.map(r => (r.bytes || 0) / (1024 * 1024));  // MB
    const reqsData  = series.map(r => r.requests || 0);

    if (this._chart) this._chart.destroy();
    this._chart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "MB transferred",
            data: bytesData,
            borderColor: "#60a5fa",
            backgroundColor: "rgba(96, 165, 250, 0.12)",
            fill: true, tension: 0.35, borderWidth: 2,
            pointRadius: 3, pointBackgroundColor: "#60a5fa",
            yAxisID: "yBytes",
          },
          {
            label: "Requests",
            data: reqsData,
            borderColor: "#a78bfa",
            backgroundColor: "rgba(167, 139, 250, 0.08)",
            fill: false, tension: 0.35, borderWidth: 1,
            borderDash: [4, 4],
            pointRadius: 2, pointBackgroundColor: "#a78bfa",
            yAxisID: "yReqs",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: {
            labels: { color: "#cbd5e1", usePointStyle: true,
                      boxWidth: 8, boxHeight: 8 },
          },
          tooltip: {
            backgroundColor: "rgba(15, 20, 25, 0.95)",
            borderColor: "rgba(99, 102, 241, 0.3)",
            borderWidth: 1,
            callbacks: {
              label: (ctx) => {
                const v = ctx.parsed.y;
                if (ctx.datasetIndex === 0)
                  return `${ctx.dataset.label}: ${v.toFixed(2)} MB`;
                return `${ctx.dataset.label}: ${v.toLocaleString()}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid:  { color: "rgba(148,163,184,0.08)" },
            ticks: { color: "#94a3b8", maxRotation: 0, autoSkipPadding: 20 },
          },
          yBytes: {
            type: "linear", position: "left",
            grid:  { color: "rgba(148,163,184,0.08)" },
            ticks: { color: "#94a3b8",
                     callback: v => `${v} MB` },
            beginAtZero: true,
          },
          yReqs: {
            type: "linear", position: "right",
            grid:  { display: false },
            ticks: { color: "#94a3b8" },
            beginAtZero: true,
          },
        },
      },
    });
  },

  _renderProfileTable(profiles) {
    const tbody = $("#tr-profile-tbody");
    if (!tbody) return;

    if (!profiles.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-state"
        style="padding: 24px; text-align: center;">
        No traffic recorded in this range. Start a profile to see stats
        appear within 30s.
      </td></tr>`;
      return;
    }

    // Find max bytes for the visual bar (relative size indicator)
    const maxBytes = Math.max(...profiles.map(p => p.bytes || 0)) || 1;

    tbody.innerHTML = profiles.map(p => {
      const pct = ((p.bytes || 0) / maxBytes * 100).toFixed(1);
      return `<tr>
        <td>
          <a href="#" onclick="Traffic._drillToProfile('${escapeHtml(p.profile_name)}'); return false;"
             class="tr-profile-link">${escapeHtml(p.profile_name)}</a>
        </td>
        <td>
          <div class="tr-bar-wrap">
            <div class="tr-bar" style="width: ${pct}%"></div>
            <span class="tr-bar-label">${formatBytes(p.bytes || 0)}</span>
          </div>
        </td>
        <td>${(p.requests || 0).toLocaleString()}</td>
        <td>${p.domain_count || 0}</td>
        <td>
          <button class="btn btn-secondary btn-small"
                  onclick="Traffic._drillToProfile('${escapeHtml(p.profile_name)}')">
            View domains →
          </button>
        </td>
      </tr>`;
    }).join("");
  },

  _renderProfileFilter(profiles) {
    const sel = $("#tr-domain-profile-filter");
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = `<option value="">All profiles</option>` +
      profiles.map(p => {
        const isSel = p.profile_name === current ? "selected" : "";
        return `<option value="${escapeHtml(p.profile_name)}" ${isSel}>
          ${escapeHtml(p.profile_name)}
        </option>`;
      }).join("");
  },

  _renderDomainTable(domains, profileFilter) {
    const tbody = $("#tr-domain-tbody");
    if (!tbody) return;

    // Update card title + profiles column header based on filter mode.
    // When filtered to one profile, "Profiles" column becomes meaningless,
    // so we drop it — that's why the <th> has an id for live update.
    const title  = $("#tr-domain-card-title");
    const profCol = $("#tr-domain-profiles-col");
    if (profileFilter) {
      if (title)  title.textContent = `Top domains for ${profileFilter}`;
      if (profCol) profCol.style.display = "none";
    } else {
      if (title)  title.textContent = "Top domains (all profiles)";
      if (profCol) profCol.style.display = "";
    }

    if (!domains.length) {
      const colspan = profileFilter ? 3 : 4;
      tbody.innerHTML = `<tr><td colspan="${colspan}" class="empty-state"
        style="padding: 24px; text-align: center;">
        No domains recorded.
      </td></tr>`;
      return;
    }

    const maxBytes = Math.max(...domains.map(d => d.bytes || 0)) || 1;

    tbody.innerHTML = domains.map(d => {
      const pct = ((d.bytes || 0) / maxBytes * 100).toFixed(1);
      const profCell = profileFilter
        ? ""  // column is hidden
        : `<td>${d.profiles || 1}</td>`;
      // Hide-the-column on filter via CSS display:none on the <td>
      const hideStyle = profileFilter ? 'style="display:none;"' : "";
      return `<tr>
        <td><code class="tr-domain">${escapeHtml(d.domain || "")}</code></td>
        <td>
          <div class="tr-bar-wrap">
            <div class="tr-bar" style="width: ${pct}%"></div>
            <span class="tr-bar-label">${formatBytes(d.bytes || 0)}</span>
          </div>
        </td>
        <td>${(d.requests || 0).toLocaleString()}</td>
        <td ${hideStyle}>${d.profiles || 1}</td>
      </tr>`;
    }).join("");
  },

  /** Called from the per-profile row "View domains →" button.
   *  Sets the filter + reloads the domains table. */
  _drillToProfile(profileName) {
    this._profileFilter = profileName;
    $("#tr-domain-profile-filter").value = profileName;
    this.loadDomains();
    // Scroll the domains card into view — the user clicked "drill" so
    // we owe them a visible result.
    document.getElementById("tr-domain-card-title")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  },

  _describeRange(hours) {
    if (hours <= 1)   return "last hour";
    if (hours <= 24)  return `last ${hours} hours`;
    const days = Math.round(hours / 24);
    return `last ${days} days`;
  },
};

// formatBytes comes from utils.js (script load order guarantees it's
// defined first). We used to re-declare a local `const formatBytes`
// here as a fallback, but that threw SyntaxError if utils.js hoisted
// its declaration to the same global scope — `const` doesn't allow
// redeclaration. Reference the global directly instead.
