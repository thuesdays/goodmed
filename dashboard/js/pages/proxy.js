// ═══════════════════════════════════════════════════════════════
// pages/proxy.js — pool config, IP statistics table, live diagnostics
// ═══════════════════════════════════════════════════════════════

const ProxyPage = {
  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // Expected values for diagnostics header
    const expCountry = getByPath(configCache, "browser.expected_country") || "Ukraine";
    const expTz      = getByPath(configCache, "browser.expected_timezone") || "Europe/Kyiv";
    $("#expected-country").textContent = expCountry;
    $("#expected-tz").textContent      = expTz;

    // Wire up buttons
    $("#btn-refresh-ip").addEventListener("click",  () => this.refreshCurrentIp());
    $("#btn-rotate-ip").addEventListener("click",   () => this.rotateIp());
    $("#btn-rotation-test").addEventListener("click", () => this.runRotationTest());
    const testApiBtn = document.getElementById("btn-test-rotation-api");
    if (testApiBtn) {
      testApiBtn.addEventListener("click", () => this.testRotationApi());
    }

    // Rotation-API config status: re-check whenever the user edits the
    // provider dropdown or the URL field, so the chip/banner update
    // live without a reload.
    document.querySelectorAll(
      '[data-config="proxy.rotation_provider"], ' +
      '[data-config="proxy.rotation_api_url"]'
    ).forEach(el => {
      el.addEventListener("input",  () => this.refreshRotationStatus());
      el.addEventListener("change", () => this.refreshRotationStatus());
    });
    this.refreshRotationStatus();

    // asocks builder — two separate fields that auto-assemble into the
    // real rotation_api_url. Matches the GET /v2/proxy/refresh/{portId}?apiKey={key}
    // endpoint shape per https://api.asocks.com/api/docs/
    const portInput = document.getElementById("asocks-port-id");
    const keyInput  = document.getElementById("asocks-api-key");
    if (portInput && keyInput) {
      // Hydrate from cache
      portInput.value = getByPath(configCache, "proxy.asocks_port_id") || "";
      keyInput.value  = getByPath(configCache, "proxy.asocks_api_key") || "";

      const rebuild = () => this.rebuildAsocksUrl();
      portInput.addEventListener("input", rebuild);
      keyInput.addEventListener("input",  rebuild);
      rebuild();    // initial assembly
    }

    // Show / hide the right builder block depending on provider choice
    const providerSelect = document.getElementById("rotation-provider-select");
    if (providerSelect) {
      providerSelect.addEventListener("change",
        () => this.toggleRotationBuilder());
      this.toggleRotationBuilder();
    }

    // Auto-detect button — fetches the port list from asocks so the
    // user doesn't have to dig for their portId manually.
    const listBtn = document.getElementById("btn-asocks-list-ports");
    if (listBtn) {
      listBtn.addEventListener("click", () => this.fetchAsocksPorts());
    }

    // Scroll-to-anchor for the banner link
    document.querySelectorAll("[data-scroll-target]").forEach(el => {
      el.addEventListener("click", e => {
        e.preventDefault();
        const target = document.getElementById(el.dataset.scrollTarget);
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.add("flash-highlight");
          setTimeout(() => target.classList.remove("flash-highlight"), 1500);
        }
      });
    });

    await Promise.all([
      this.loadIps(),
      this.refreshCurrentIp(),   // show current IP right away
    ]);
  },

  /** Updates the chip in the Rotation-API card header and the warning
   *  banner in Live diagnostics. Call this any time the user edits
   *  provider / URL. */
  refreshRotationStatus() {
    // configCache is nested: { proxy: { rotation_provider: ..., ... } }
    // Use getByPath (from config-form.js) so we respect that shape.
    const provider = getByPath(configCache, "proxy.rotation_provider") || "none";
    const url      = (getByPath(configCache, "proxy.rotation_api_url") || "").trim();
    const configured = provider !== "none" && !!url;

    const chip = document.getElementById("rotation-status-chip");
    if (chip) {
      chip.classList.toggle("on",  configured);
      chip.classList.toggle("off", !configured);
      chip.textContent = configured
        ? `✓ ${provider}`
        : "not configured";
    }

    const banner = document.getElementById("rotation-missing-banner");
    if (banner) {
      banner.style.display = configured ? "none" : "";
    }
  },

  /** Show the asocks simple-fields block when provider=asocks, the
   *  generic URL editor otherwise. Keeps the two paths from colliding. */
  toggleRotationBuilder() {
    const provider = getByPath(configCache, "proxy.rotation_provider") || "none";
    const asocksBox   = document.getElementById("rotation-asocks-builder");
    const advancedBox = document.getElementById("rotation-advanced-builder");
    if (!asocksBox || !advancedBox) return;

    if (provider === "asocks") {
      asocksBox.style.display   = "";
      advancedBox.style.display = "none";
      // Make sure the URL reflects the asocks inputs
      this.rebuildAsocksUrl();
    } else if (provider === "none") {
      asocksBox.style.display   = "none";
      advancedBox.style.display = "none";
    } else {
      asocksBox.style.display   = "none";
      advancedBox.style.display = "";
    }
  },

  /** Call /v2/proxy/port-list on asocks and show the result as a
   *  clickable list. The user picks a row → its portId goes into
   *  the Port ID input and URL is rebuilt automatically. */
  async fetchAsocksPorts() {
    const btn    = document.getElementById("btn-asocks-list-ports");
    const list   = document.getElementById("asocks-port-list");
    const keyInp = document.getElementById("asocks-api-key");
    if (!btn || !list || !keyInp) return;

    const apiKey = (keyInp.value || "").trim();
    if (!apiKey) {
      list.innerHTML = `<div class="asocks-port-err">
        Fill in the API key first.
      </div>`;
      list.style.display = "";
      return;
    }

    btn.disabled = true;
    btn.textContent = "⏳ Fetching…";
    list.innerHTML = `<div class="asocks-port-loading">
      Calling asocks API…
    </div>`;
    list.style.display = "";

    try {
      const r = await api("/api/proxy/asocks-port-list", {
        method: "POST",
        body:   JSON.stringify({ api_key: apiKey }),
      });

      if (!r.ok) {
        list.innerHTML = `<div class="asocks-port-err">
          ✗ ${escapeHtml(r.error || "failed")} ${r.http ? `(HTTP ${r.http})` : ""}
          ${r.body ? `<pre>${escapeHtml(r.body)}</pre>` : ""}
        </div>`;
        return;
      }
      if (!r.ports.length) {
        list.innerHTML = `<div class="asocks-port-err">
          No ports found on your asocks account. Create one in the
          asocks dashboard first.
        </div>`;
        return;
      }
      this._renderAsocksPorts(r.ports);
    } catch (e) {
      list.innerHTML = `<div class="asocks-port-err">
        ✗ ${escapeHtml(e.message || "request failed")}
      </div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "🔍 Fetch port list from asocks";
    }
  },

  _renderAsocksPorts(ports) {
    const list = document.getElementById("asocks-port-list");
    const rows = ports.map(p => {
      const hostPort = p.host && p.port ? `${p.host}:${p.port}` : "—";
      const country  = p.country ? escapeHtml(p.country) : "—";
      const city     = p.city    ? ` · ${escapeHtml(p.city)}` : "";
      const name     = p.name    ? escapeHtml(p.name) : "(unnamed)";
      // asocks hands us a pre-signed rotation URL per port. Stash it on
      // the row so "Use this" can apply it directly — no need for the
      // user to have the apiKey field populated to assemble the URL.
      const refreshAttr = p.refresh_link
        ? ` data-refresh-link="${escapeHtml(p.refresh_link)}"`
        : "";
      return `
        <div class="asocks-port-row"
             data-port-id="${escapeHtml(String(p.id))}"${refreshAttr}>
          <div class="asocks-port-row-main">
            <code class="asocks-port-id-chip">id: ${escapeHtml(String(p.id))}</code>
            <div>
              <div class="asocks-port-name">${name}</div>
              <div class="asocks-port-meta">
                ${country}${city} · <code>${escapeHtml(hostPort)}</code>
              </div>
            </div>
          </div>
          <button class="btn btn-primary btn-small asocks-port-pick">
            Use this
          </button>
        </div>`;
    }).join("");

    list.innerHTML = `
      <div class="asocks-port-title">
        ✓ Found ${ports.length} port${ports.length === 1 ? "" : "s"} on your account —
        click <strong>Use this</strong> next to the one you want to rotate:
      </div>
      ${rows}`;

    list.querySelectorAll(".asocks-port-row").forEach(row => {
      row.addEventListener("click", () => {
        const portId      = row.dataset.portId;
        const refreshLink = row.dataset.refreshLink;
        const portInput   = document.getElementById("asocks-port-id");

        if (portInput) {
          portInput.value = portId;
        }

        // If asocks gave us a ready-to-use refresh_link, use IT as the
        // rotation URL directly. It already contains the apiKey encoded
        // as a query param — no need to rebuild.
        if (refreshLink) {
          setByPath(configCache, "proxy.rotation_api_url",  refreshLink);
          setByPath(configCache, "proxy.asocks_port_id",    portId);
          setByPath(configCache, "proxy.rotation_method",   "GET");
          setByPath(configCache, "proxy.rotation_api_key",  null);

          const preview = document.getElementById("asocks-assembled-url");
          if (preview) {
            // Mask the key portion for safe screenshots
            const masked = refreshLink.replace(
              /(apiKey=)([^&]+)/,
              (_, k, v) => k + v.slice(0, 6) + "…" + v.slice(-4)
            );
            preview.textContent = masked;
            preview.classList.remove("muted");
          }
          scheduleConfigSave();
          this.refreshRotationStatus();
        } else {
          // Fallback: trigger the old rebuild path (asocks didn't give
          // us a link, for whatever reason).
          portInput?.dispatchEvent(new Event("input"));
        }

        // Highlight selection
        list.querySelectorAll(".asocks-port-row").forEach(r =>
          r.classList.toggle("selected", r === row)
        );
      });
    });
  },

  /** Assemble the real rotation URL from the asocks portId + apiKey
   *  fields, persist it in configCache, and update the preview. */
  rebuildAsocksUrl() {
    const portInput = document.getElementById("asocks-port-id");
    const keyInput  = document.getElementById("asocks-api-key");
    const preview   = document.getElementById("asocks-assembled-url");
    if (!portInput || !keyInput) return;

    const portId = (portInput.value || "").trim();
    const apiKey = (keyInput.value || "").trim();

    // Persist the two fragments separately so the user doesn't have to
    // retype them next visit.
    setByPath(configCache, "proxy.asocks_port_id", portId || null);
    setByPath(configCache, "proxy.asocks_api_key", apiKey || null);

    let url = "";
    if (portId && apiKey) {
      url = `https://api.asocks.com/v2/proxy/refresh/${encodeURIComponent(portId)}` +
            `?apiKey=${encodeURIComponent(apiKey)}`;
    }

    // Write the assembled URL into the same config key backend uses
    setByPath(configCache, "proxy.rotation_api_url", url || null);
    // asocks expects GET and carries auth in the URL — enforce both
    setByPath(configCache, "proxy.rotation_method",  "GET");
    setByPath(configCache, "proxy.rotation_api_key", null);

    if (preview) {
      if (url) {
        // Mask the key in the on-screen preview so the user can share
        // screenshots without leaking the token.
        const masked = url.replace(
          /(apiKey=)([^&]+)/,
          (_, k, v) => k + v.slice(0, 6) + "…" + v.slice(-4)
        );
        preview.textContent = masked;
        preview.classList.remove("muted");
      } else {
        preview.textContent = "Fill in Port ID and API key above to assemble the URL.";
        preview.classList.add("muted");
      }
    }

    scheduleConfigSave();
    this.refreshRotationStatus();
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
      const before = $("#proxy-live-ip").textContent.trim();
      const info   = await api("/api/proxy/rotate", { method: "POST" });

      // rotation_called === false means the API wasn't configured or
      // call failed — tell the user exactly what's wrong.
      if (info.rotation_called === false) {
        toast(
          info.rotation_error || "Rotation not triggered — check config",
          true
        );
      }

      await this.refreshCurrentIp();
      const after = $("#proxy-live-ip").textContent.trim();

      if (info.rotation_called) {
        if (before && after && before !== after) {
          toast(`✓ Rotated: ${before} → ${after}`);
        } else {
          toast(
            "⚠ Rotation API called successfully but IP didn't change. " +
            "Provider may be slow or reissued the same IP.",
            true
          );
        }
      }
    } catch (e) {
      toast(e.message || "rotation failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "⚡ Rotate IP";
    }
  },

  /** One-off ping against the configured rotation URL — shows HTTP
   *  status + response so the user can confirm credentials are valid. */
  async testRotationApi() {
    const btn = document.getElementById("btn-test-rotation-api");
    const out = document.getElementById("rotation-api-test-result");
    btn.disabled = true;
    btn.textContent = "⏳ Testing…";
    out.textContent = "Calling rotation API…";
    out.className = "form-hint";
    try {
      const r = await api("/api/proxy/test-rotation-api", { method: "POST" });
      out.textContent = r.message || (r.ok ? "ok" : "failed");
      out.className = r.ok
        ? "form-hint form-hint-success"
        : "form-hint form-hint-error";
    } catch (e) {
      out.textContent = `✗ Request failed: ${e.message}`;
      out.className = "form-hint form-hint-error";
    } finally {
      btn.disabled = false;
      btn.textContent = "🧪 Test rotation API";
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

    const expCountry = (getByPath(configCache, "browser.expected_country") || "Ukraine").toLowerCase();
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
