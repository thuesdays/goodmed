// ═══════════════════════════════════════════════════════════════
// pages/proxy.js — pool config, IP statistics table, live diagnostics
// ═══════════════════════════════════════════════════════════════

const ProxyPage = {
  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // Expected values for diagnostics header
    const expCountry = configCache["browser.expected_country"] || "Ukraine";
    const expTz      = configCache["browser.expected_timezone"] || "Europe/Kyiv";
    $("#expected-country").textContent = expCountry;
    $("#expected-tz").textContent      = expTz;

    // Wire up buttons
    $("#btn-refresh-ip").addEventListener("click",  () => this.refreshCurrentIp());
    $("#btn-rotate-ip").addEventListener("click",   () => this.rotateIp());
    $("#btn-rotation-test").addEventListener("click", () => this.runRotationTest());

    await Promise.all([
      this.loadIps(),
      this.refreshCurrentIp(),   // show current IP right away
    ]);
  },

  // ───────────────────────────────────────────────────────────
  // Current IP panel
  // ───────────────────────────────────────────────────────────

  async refreshCurrentIp() {
    const btn = $("#btn-refresh-ip");
    btn.disabled = true;
    btn.textContent = "⏳ Checking…";
    try {
      const report = await api("/api/proxy/full-diagnostics", { method: "POST" });
      this._renderCurrentIp(report);
    } catch (e) {
      this._renderError(e.message || "diagnostics failed");
    } finally {
      btn.disabled = false;
      btn.textContent = "🔄 Refresh";
    }
  },

  async rotateIp() {
    const btn = $("#btn-rotate-ip");
    btn.disabled = true;
    btn.textContent = "⏳ Rotating…";
    try {
      const info = await api("/api/proxy/rotate", { method: "POST" });
      // Build a report-like object for _renderCurrentIp
      await this.refreshCurrentIp();
      if (info.ok) {
        toast(`New IP: ${info.ip} (${info.country || "?"})`);
      } else {
        toast(info.error || "rotation failed", true);
      }
    } catch (e) {
      toast(e.message || "rotation failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "⚡ Rotate IP";
    }
  },

  _renderCurrentIp(report) {
    if (!report.ok) {
      this._renderError(report.error || "lookup failed");
      return;
    }
    const ip = report.ip || {};
    const flag = countryFlag(ip.country_code);

    $("#proxy-live-ip").innerHTML      = `${flag} ${escapeHtml(ip.ip || "—")}`;
    $("#proxy-live-country").textContent = ip.country || "—";
    $("#proxy-live-city").textContent    = ip.city    || "—";
    $("#proxy-live-tz").textContent      = ip.timezone || "—";
    $("#proxy-live-asn").textContent     = ip.org     || "—";

    // IP type with coloring
    const typeEl = $("#proxy-live-iptype");
    typeEl.textContent = report.ip_type || "unknown";
    typeEl.className   = "proxy-live-cell-value iptype-" + (report.ip_type || "unknown");

    // Risk with coloring
    const riskEl = $("#proxy-live-risk");
    riskEl.textContent = report.detection_risk || "medium";
    riskEl.className   = "proxy-live-cell-value risk-" + (report.detection_risk || "medium");

    // Check rows
    this._renderCheck("check-geo", report.geo_match,
      report.geo_match
        ? `Geo match: ${ip.country}`
        : `Geo MISMATCH: got ${ip.country}, expected ${report.expected_country}`);
    this._renderCheck("check-tz", report.tz_match,
      report.tz_match
        ? `Timezone match: ${ip.timezone}`
        : `Timezone MISMATCH: got ${ip.timezone}, expected ${report.expected_timezone}`);
  },

  _renderCheck(elId, passed, text) {
    const el = $("#" + elId);
    el.className = "proxy-live-check " + (passed ? "pass" : "fail");
    el.querySelector(".check-icon").textContent = passed ? "✓" : "✗";
    el.querySelector("span:last-child").textContent = text;
  },

  _renderError(msg) {
    $("#proxy-live-ip").textContent       = "error";
    $("#proxy-live-country").textContent  = "—";
    $("#proxy-live-city").textContent     = "—";
    $("#proxy-live-tz").textContent       = "—";
    $("#proxy-live-asn").textContent      = msg.slice(0, 60);
    $("#proxy-live-iptype").textContent   = "—";
    $("#proxy-live-risk").textContent     = "—";
    this._renderCheck("check-geo", false, `Check failed: ${msg.slice(0, 80)}`);
    this._renderCheck("check-tz",  false, "Not checked");
  },

  // ───────────────────────────────────────────────────────────
  // Rotation test
  // ───────────────────────────────────────────────────────────

  async runRotationTest() {
    const btn = $("#btn-rotation-test");
    const container = $("#rotation-test-results");
    const n = parseInt($("#rotation-count").value, 10);

    btn.disabled = true;
    btn.textContent = `⏳ Testing ${n} requests…`;
    container.innerHTML = `
      <div class="rotation-progress">
        <div class="rotation-progress-label">
          Running ${n} requests through the proxy…
        </div>
        <div class="rotation-progress-bar"><div class="rotation-progress-fill"></div></div>
      </div>
    `;

    try {
      const resp = await api("/api/proxy/test-rotation", {
        method: "POST",
        body: JSON.stringify({ count: n }),
      });

      if (!resp.ok) {
        container.innerHTML = `<div class="error-banner">✗ ${escapeHtml(resp.error || "test failed")}</div>`;
        return;
      }
      this._renderRotationResults(resp);
    } catch (e) {
      container.innerHTML = `<div class="error-banner">✗ ${escapeHtml(e.message || "error")}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "🧪 Run test";
    }
  },

  _renderRotationResults(resp) {
    const container = $("#rotation-test-results");

    // Summary: country breakdown
    const total = resp.total;
    const countryEntries = Object.entries(resp.countries)
      .sort((a, b) => b[1] - a[1]);

    const summaryHtml = `
      <div class="rotation-summary">
        <div class="rotation-summary-row">
          <span class="rotation-summary-label">Unique IPs</span>
          <span class="rotation-summary-value">${resp.unique_ips} / ${total}</span>
        </div>
        <div class="rotation-summary-row">
          <span class="rotation-summary-label">Unique countries</span>
          <span class="rotation-summary-value">${countryEntries.length}</span>
        </div>
        ${countryEntries.map(([c, n]) => {
          const pct = Math.round(100 * n / total);
          return `
            <div class="rotation-country-bar">
              <div class="rotation-country-name">${escapeHtml(c)}</div>
              <div class="rotation-country-track">
                <div class="rotation-country-fill" style="width: ${pct}%"></div>
              </div>
              <div class="rotation-country-count">${n} (${pct}%)</div>
            </div>
          `;
        }).join("")}
      </div>
    `;

    const expCountry = (configCache["browser.expected_country"] || "Ukraine").toLowerCase();
    const rowsHtml = resp.results.map((r, i) => {
      if (!r.ok) {
        return `<tr class="rotation-row-err">
          <td>${i + 1}</td>
          <td colspan="4" class="muted">✗ ${escapeHtml(r.error || "lookup failed")}</td>
        </tr>`;
      }
      const countryOk = (r.country || "").toLowerCase().includes(expCountry) ||
                        expCountry.includes((r.country || "").toLowerCase());
      const flag = countryFlag(r.country_code);
      return `
        <tr class="${countryOk ? "rotation-row-ok" : "rotation-row-warn"}">
          <td>${i + 1}</td>
          <td><code>${escapeHtml(r.ip || "?")}</code></td>
          <td>${flag} ${escapeHtml(r.country || "?")}</td>
          <td>${escapeHtml(r.city || "?")}</td>
          <td class="muted">${escapeHtml((r.org || "?").slice(0, 50))}</td>
        </tr>
      `;
    }).join("");

    container.innerHTML = `
      ${summaryHtml}
      <table class="rotation-results-table">
        <thead>
          <tr>
            <th>#</th><th>IP</th><th>Country</th><th>City</th><th>Provider</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    `;
  },

  // ───────────────────────────────────────────────────────────
  // IP statistics table
  // ───────────────────────────────────────────────────────────

  async loadIps() {
    try {
      const ips = await api("/api/ips");
      $("#ip-count").textContent = ips.length;

      const tbody = $("#ips-tbody");
      if (!ips.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No IP data yet</td></tr>`;
        return;
      }

      tbody.innerHTML = ips.map(ip => `
        <tr>
          <td><strong>${escapeHtml(ip.ip)}</strong></td>
          <td class="muted">${escapeHtml(ip.country || "—")}</td>
          <td class="muted">${escapeHtml(ip.org || "—")}</td>
          <td>${ip.total_uses}</td>
          <td>${ip.total_captchas}</td>
          <td>${(ip.captcha_rate * 100).toFixed(1)}%</td>
          <td><span class="pill pill-${ip.status}">${ip.status}</span></td>
        </tr>
      `).join("");
    } catch (e) {
      console.error(e);
    }
  },
};

// Helper: flag emoji from country code (e.g. "UA" → "🇺🇦")
function countryFlag(code) {
  if (!code || code.length !== 2) return "🌐";
  const codePoints = code.toUpperCase()
    .split("")
    .map(c => 127397 + c.charCodeAt(0));
  return String.fromCodePoint(...codePoints);
}
