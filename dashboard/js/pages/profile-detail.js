// ═══════════════════════════════════════════════════════════════
// pages/profile-detail.js
// ═══════════════════════════════════════════════════════════════

const ProfileDetail = {
  currentProfile: null,

  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // Pick profile from URL hash (#profile?name=...) first; fall back to
    // the active profile in config. The dropdown picker is gone — users
    // navigate here from the Profiles page per-row, so the URL is the
    // single source of truth for which profile we're editing.
    const params = new URLSearchParams(
      (location.hash.split("?")[1] || "")
    );
    const fromHash = params.get("name");
    const fromCache = configCache?.browser?.profile_name;
    this.currentProfile = fromHash || fromCache || null;
    this._renderHeader(this.currentProfile);

    // Wire the rotation-toggle checkbox in the new Proxy card so the
    // rotation block expands/collapses cleanly. Default = collapsed.
    document.getElementById("pp-proxy-rotating")
      ?.addEventListener("change", (e) => {
        const block = document.getElementById("pp-rotation-block");
        if (block) block.style.display = e.target.checked ? "" : "none";
      });
    // Separate Save button on the Proxy card — distinct from the
    // Identity card's Save (different scope, less surprising for users).
    document.getElementById("pp-proxy-save-btn-new")
      ?.addEventListener("click", () => this.saveProfileMeta());
    document.getElementById("pp-proxy-test-btn-new")
      ?.addEventListener("click", () => this._testProxyOverride());

    $("#reset-health-btn").addEventListener("click", () => this.resetHealth());
    $("#clear-history-btn").addEventListener("click", () => this.clearHistory());
    $("#delete-profile-btn").addEventListener("click", () => this.deleteProfile());

    // ── Sprint 8: Backup / Restore wiring ──
    // Two buttons on the profile-backup card open one of two modals.
    // The modals are completely self-contained — open / inspect /
    // submit / close cycle is driven by these handlers only. We never
    // cache the master password in JS state — it's read straight off
    // the input on submit, posted, then the input is cleared on close.
    document.getElementById("profile-backup-btn")
      ?.addEventListener("click", () => this._openBackupModal());
    document.getElementById("profile-restore-btn")
      ?.addEventListener("click", () => this._openRestoreModal());
    document.getElementById("profile-backup-submit")
      ?.addEventListener("click", () => this._submitBackup());
    document.getElementById("profile-restore-file")
      ?.addEventListener("change", (e) =>
        this._onRestoreFilePicked(e.target.files[0]));
    document.getElementById("profile-restore-inspect-btn")
      ?.addEventListener("click", () => this._submitInspect());
    document.getElementById("profile-restore-submit-btn")
      ?.addEventListener("click", () => this._submitRestore());
    document.getElementById("profile-restore-back-btn")
      ?.addEventListener("click", () => this._restoreBackToPick());
    // Close-on-backdrop / × button for backup modals
    document.querySelectorAll(
      '[data-close="profile-backup-modal"], [data-close="profile-restore-modal"], [data-close="profile-cloud-restore-modal"]'
    ).forEach(el => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-close");
        const m = document.getElementById(id);
        if (m) m.style.display = "none";
        if (id === "profile-backup-modal") this._resetBackupModal();
        if (id === "profile-restore-modal") this._resetRestoreModal();
      });
    });

    // Sprint 8.2 — cloud sync. Buttons start hidden until the
    // GET /api/backup/sync/test probe confirms a target is wired up.
    document.getElementById("profile-backup-cloud-push-btn")
      ?.addEventListener("click", () => this._submitCloudPush());
    document.getElementById("profile-backup-cloud-restore-btn")
      ?.addEventListener("click", () => this._openCloudRestoreModal());
    this._probeCloudSync();

    // Active-script selector — load the scripts list + current assignment
    document.getElementById("profile-script-save-btn")
      ?.addEventListener("click", () => this.saveActiveScript());

    // Active-proxy selector — mirror of scripts assignment
    document.getElementById("profile-proxy-save-btn")
      ?.addEventListener("click", () => this.saveActiveProxy());
    document.getElementById("profile-proxy-test-btn")
      ?.addEventListener("click", () => this.testActiveProxy());

    // Per-profile overrides wiring (tags, proxy, rotation, notes)
    document.getElementById("pp-save-btn")
      ?.addEventListener("click", () => this.saveProfileMeta());
    document.getElementById("pp-tag-add-btn")
      ?.addEventListener("click", () => this._addTag());
    document.getElementById("pp-tag-input")
      ?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); this._addTag(); }
      });

    // ── Cookie management buttons ──
    document.getElementById("cookies-reload-btn")
      ?.addEventListener("click", () => this.loadCookies(this.currentProfile));
    document.getElementById("cookies-import-btn")
      ?.addEventListener("click", () => this._openCookieImport());
    document.getElementById("cookies-export-btn")
      ?.addEventListener("click", () => this._exportCookies());
    document.getElementById("cookies-clear-btn")
      ?.addEventListener("click", () => this._clearCookies());
    document.getElementById("cookies-search")
      ?.addEventListener("input", (e) => {
        this._cookieFilter = (e.target.value || "").toLowerCase();
        this._renderCookies();
      });

    // Cookie import modal wiring
    document.querySelectorAll('[data-close="cookie-import-modal"]').forEach(el => {
      el.addEventListener("click", () => this._closeCookieImport());
    });
    document.getElementById("cookie-import-file")
      ?.addEventListener("change", (e) => this._handleCookieFile(e.target.files[0]));
    document.getElementById("cookie-import-submit")
      ?.addEventListener("click", () => this._submitCookieImport());

    const regenBtn = document.getElementById("regen-fp-btn");
    if (regenBtn) {
      regenBtn.addEventListener("click", () => this.regenerateFingerprint());
    }

    // New Phase-2 card: Quick regenerate button on the active-fp card
    document.getElementById("profile-fp-regen-btn")
      ?.addEventListener("click", () => this.quickRegenerateFingerprint());

    // ── Chrome history importer ──
    // Auto-detect the source path on page load so the input is pre-filled.
    this._populateChromeImportSource();
    document.getElementById("chrome-import-run-btn")
      ?.addEventListener("click", () => this._runChromeImport());

    // Initial data load for the picked profile. If we somehow landed
    // here without a name (deep-linked from another tool), bail loudly
    // — the page shows an empty state, and a Back button.
    if (this.currentProfile) {
      await Promise.all([
        this.loadSelfcheck(this.currentProfile),
        this.loadFingerprint(this.currentProfile),
        this.loadSessionSummary(this.currentProfile),
        this.loadProfileMeta(this.currentProfile),
        this.loadCookies(this.currentProfile),
        this.loadActiveScript(this.currentProfile),
        this.loadActiveProxy(this.currentProfile),
        this.loadProfileHealth(this.currentProfile),
        this.loadCaptchaHistory(this.currentProfile),
        // Extensions card (Phase 3): list assigned chips + wire the
        // "+ Add from pool" picker. Stays a no-op for users who never
        // touched Extensions — chips just render as "(none assigned)".
        this.loadProfileExtensions(this.currentProfile),
      ]);
      this._wireProfileExtBtn();
    } else {
      toast("No profile selected — redirecting to the Profiles list", true);
      if (typeof navigate === "function") {
        setTimeout(() => navigate("profiles"), 600);
      }
    }
  },

  /** Fill the page header's "Edit profile: <name>" label.
   *  Called once on init — the page reloads (different name) come via
   *  navigation from the Profiles page, not via in-page state. */
  _renderHeader(name) {
    const sep = document.getElementById("profile-name-sep");
    const lbl = document.getElementById("profile-name-display");
    if (!lbl) return;
    if (name) {
      sep.textContent = ": ";
      lbl.textContent = name;
    } else {
      sep.textContent = "";
      lbl.textContent = "";
    }
  },

  /**
   * D5-UI: render the needs_attention banner for the given profile
   * + meta payload. Toggles visibility based on meta.needs_attention,
   * fills the reason / timestamp from meta.needs_attention_reason /
   * meta.needs_attention_at, and wires the "Clear attention" button
   * to /api/profiles/<name>/clear-attention.
   *
   * Defensive: if any DOM hook is missing (page never rendered the
   * banner template), this silently no-ops.
   */
  _renderAttentionBanner(name, meta) {
    const banner = document.getElementById("pp-attention-banner");
    const reason = document.getElementById("pp-attention-reason");
    const when   = document.getElementById("pp-attention-when");
    const btn    = document.getElementById("pp-attention-clear-btn");
    const status = document.getElementById("pp-attention-clear-status");
    if (!banner || !reason || !when || !btn) return;

    const flag = !!(meta && Number(meta.needs_attention) === 1);
    if (!flag) {
      banner.style.display = "none";
      return;
    }

    reason.textContent = (meta.needs_attention_reason
                          || "Profile flagged — see logs for details.");
    if (meta.needs_attention_at) {
      // ISO timestamp from main.py: "2026-04-29T10:51:15".
      // Render it readably without leaking the seconds noise.
      try {
        const d = new Date(meta.needs_attention_at);
        if (!isNaN(d.getTime())) {
          when.textContent = "Flagged at: " + d.toLocaleString();
        } else {
          when.textContent = "Flagged at: " + meta.needs_attention_at;
        }
      } catch (_e) {
        when.textContent = "Flagged at: " + meta.needs_attention_at;
      }
    } else {
      when.textContent = "";
    }
    banner.style.display = "";

    // Wire the Clear button — idempotent: tag once, never re-bind.
    // Re-binding on every meta-load (which happens whenever the page
    // re-renders) would stack handlers and POST N times per click.
    if (btn.dataset._wired === "1") return;
    btn.dataset._wired = "1";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      if (status) { status.textContent = "Clearing…"; status.style.color = ""; }
      try {
        const r = await api(
          `/api/profiles/${encodeURIComponent(name)}/clear-attention`,
          { method: "POST" }
        );
        if (r && r.ok) {
          if (status) {
            status.textContent = "Cleared. Scheduler will retry on the "
                                 + "next tick.";
            status.style.color = "#7fff7f";
          }
          // Hide banner after a short pause so the user sees the
          // confirmation; a fresh page load (or next meta load) will
          // pick up the new state from the server.
          setTimeout(() => { banner.style.display = "none"; }, 1500);
        } else {
          if (status) {
            status.textContent = "Clear failed: "
                                 + ((r && r.error) || "unknown error");
            status.style.color = "#ff8080";
          }
          btn.disabled = false;
        }
      } catch (e) {
        if (status) {
          status.textContent = "Clear failed: " + (e?.message || e);
          status.style.color = "#ff8080";
        }
        btn.disabled = false;
      }
    });
  },

  /** Test button on the Proxy card. Just hits ipinfo via the existing
   *  /api/proxy/test endpoint, passing the proxy URL we have in the form
   *  rather than the global one. */
  async _testProxyOverride() {
    const url = (document.getElementById("pp-proxy-url")?.value || "").trim();
    if (!url) {
      toast("Set a proxy URL first, then test", true);
      return;
    }
    const status = document.getElementById("pp-proxy-save-status");
    if (status) { status.textContent = "Testing…"; status.className = "muted"; }
    try {
      const r = await api("/api/proxy/test-url", {
        method: "POST", body: JSON.stringify({ url }),
      });
      if (r.ok) {
        if (status) {
          status.textContent = `OK · ${r.exit_ip || "?"} · ${r.country || "?"} · ${r.latency_ms || "?"}ms`;
          status.className = "muted profile-proxy-test-ok";
        }
      } else {
        if (status) {
          status.textContent = `FAIL · ${r.error || "unreachable"}`;
          status.className = "muted profile-proxy-test-fail";
        }
      }
    } catch (e) {
      if (status) {
        status.textContent = `FAIL · ${e.message}`;
        status.className = "muted profile-proxy-test-fail";
      }
    }
  },

  async loadSelfcheck(name) {
    try {
      const sc = await api(`/api/profiles/${encodeURIComponent(name)}/selfcheck`);
      // Backend now returns 200 with {empty:true, message:...} when
      // there is no snapshot yet -- saves the DevTools console from
      // the noisy "GET /selfcheck 404" line on every fresh-profile
      // page load.
      if (sc && sc.empty) {
        // Empty network-selfcheck. But we may still have FINGERPRINT
        // coherence data from the validator (writes on every browser
        // launch). Use that to populate the grid so the card isn't
        // blank for fresh / never-monitored profiles.
        await this._renderSelfcheckFromCoherence(name);
      } else {
        $("#selfcheck-badge").textContent = `${sc.passed}/${sc.total}`;
        $("#selfcheck-time").textContent  = `Last check: ${sc.timestamp || "—"}`;
        const tests = sc.tests || {};
        const items = Object.entries(tests).map(([testName, result]) => {
          const ok = result === true;
          return `
            <div class="selfcheck-item ${ok ? 'pass' : 'fail'}">
              <span class="icon">${ok ? '✓' : '✗'}</span>
              <span>${escapeHtml(testName)}</span>
            </div>
          `;
        }).join("");
        $("#selfcheck-grid").innerHTML = items || '<div class="empty-state">No data</div>';
      }
    } catch (e) {
      $("#selfcheck-badge").textContent = "—";
      $("#selfcheck-grid").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
    }
    // Wire Probe button on first selfcheck load (idempotent).
    this._wireFpProbeButton();
  },

  // Fallback render path: when the network-selfcheck table has no
  // row yet (typical for a freshly-created profile that's never
  // been monitored), we still have the validator's per-test report
  // from the most recent fingerprint save. That covers every signal
  // an external tester would also examine -- UA coherence, GPU
  // plausibility, font-count vs platform, timezone vs language, etc.
  // Showing this in the Self-Check grid means the card has useful
  // content from the very first time the user opens the page after
  // launching their profile, not days later after a real monitor
  // pass has run.
  async _renderSelfcheckFromCoherence(name) {
    try {
      const c = await api(
        `/api/profiles/${encodeURIComponent(name)}/fingerprint/coherence`);
      if (!c || c.empty || !c.results || !c.results.length) {
        // Nothing to show -- profile has never been launched, so no
        // FP snapshot exists either. Keep the original message.
        $("#selfcheck-badge").textContent = "—";
        $("#selfcheck-time").textContent  = "";
        $("#selfcheck-grid").innerHTML =
          `<div class="empty-state">
             ${escapeHtml((c && c.message) ||
               "No selfcheck data yet. Launch the profile once to score it.")}
           </div>`;
        return;
      }
      const passed = c.results.filter(r => r.status === "pass").length;
      const total  = c.results.length;
      const score  = c.score != null ? `${c.score}/100` : "—";
      const grade  = c.grade ? ` (${c.grade})` : "";
      $("#selfcheck-badge").textContent = `${passed}/${total}`;
      $("#selfcheck-time").textContent  =
        `Fingerprint validator: score ${score}${grade}`
        + (c.timestamp ? `  ·  last update ${c.timestamp}` : "");

      // Render each validator check as a selfcheck-item, color-coded
      // by status. Tooltip shows the validator's reason text.
      $("#selfcheck-grid").innerHTML = c.results.map(r => {
        const ico = r.status === "pass" ? "✓"
                  : r.status === "warn" ? "⚠"
                  : r.status === "fail" ? "✗" : "·";
        const cl  = r.status === "pass" ? "pass"
                  : r.status === "fail" ? "fail" : "warn";
        return `<div class="selfcheck-item ${cl}"
                     title="${escapeHtml(r.detail || '')}">
          <span class="icon">${ico}</span>
          <span>${escapeHtml(r.name)}</span>
        </div>`;
      }).join("");
    } catch (e) {
      $("#selfcheck-badge").textContent = "—";
      $("#selfcheck-grid").innerHTML =
        `<div class="empty-state">${escapeHtml(e.message || e)}</div>`;
    }
  },

  // ── Fingerprint probe button ───────────────────────────────────
  // POSTs to /api/profiles/<name>/fingerprint/probe which spawns a
  // browser run that visits the canonical external testers
  // (CreepJS, BotD, Pixelscan, AmIUnique, BrowserLeaks, FP-com BotD)
  // in the profile's actual Chrome session. The profile picks up
  // each tester's cookies + the user can review the on-page scores.
  _wireFpProbeButton() {
    const btn = document.getElementById("fp-probe-btn");
    if (!btn || btn.dataset._wired === "1") return;
    btn.dataset._wired = "1";
    btn.addEventListener("click", () => this._runFpProbe());
    // Render tester cards once -- list is static, hand-curated below.
    this._renderTesterCards();
    // Wire the modal close handlers (delegated -- cheap).
    document.querySelectorAll('[data-close="fp-tester-modal"]').forEach(el => {
      el.addEventListener("click", () => {
        const m = document.getElementById("fp-tester-modal");
        if (m) m.style.display = "none";
      });
    });
  },

  // Catalogue of external testers + what each measures + which
  // coherence-validator categories are most relevant for each.
  // Drives the modal: "Pixelscan checks canvas/webgl uniqueness ->
  // here are this profile's canvas + gpu coherence results."
  FP_TESTERS: {
    creepjs: {
      icon:        "🕵",
      label:       "CreepJS",
      url:         "https://abrahamjuliot.github.io/creepjs/",
      tagline:     "Trust score 0-100, the strictest grader.",
      description: "Detects mismatches between every browser API surface " +
                   "(UA-CH, navigator, canvas, audio, font, WebGL, Workers, " +
                   "iframes). Compares them all to expected combinations " +
                   "and outputs a 'lies score' + 'trust score'. If your " +
                   "fingerprint disagrees with itself anywhere, CreepJS " +
                   "spots it.",
      checks:      ["UA / navigator", "Canvas hash", "Audio context",
                    "Fonts", "WebGL", "Workers", "Iframes", "Lies"],
      relevant:    ["critical", "ua", "ua-ch", "canvas", "audio",
                    "fonts", "gpu"],
    },
    sannysoft: {
      icon:        "🤖",
      label:       "Sannysoft Bot Test",
      url:         "https://bot.sannysoft.com/",
      tagline:     "The classic Selenium leak panel.",
      description: "Original bot-detection test page. Checks webdriver " +
                   "flag, plugin count + names, language array, " +
                   "permissions API, iframe content-window contradictions, " +
                   "and a few more low-hanging-fruit signals selenium-stealth " +
                   "has historically tried to hide.",
      checks:      ["webdriver", "Plugins / mimeTypes", "Languages",
                    "Permissions", "WebGL Vendor / Renderer", "iframe.contentWindow"],
      relevant:    ["critical", "ua", "plugins", "languages", "gpu"],
    },
    pixelscan: {
      icon:        "🎨",
      label:       "Pixelscan",
      url:         "https://pixelscan.net/",
      tagline:     "Canvas/WebGL hash uniqueness + geo correlation.",
      description: "Computes a fingerprint hash from canvas + WebGL + " +
                   "audio renderings and reports how unique you are " +
                   "in their database. Also cross-checks declared " +
                   "timezone vs IP geolocation -- a mismatch here is a " +
                   "stronger flag than any single canvas leak.",
      checks:      ["Canvas hash", "WebGL hash", "Audio hash",
                    "Timezone vs IP geo", "Language vs IP locale", "Uniqueness rank"],
      relevant:    ["critical", "canvas", "gpu", "timezone", "languages"],
    },
    amiunique: {
      icon:        "🔍",
      label:       "AmIUnique",
      url:         "https://amiunique.org/fingerprint",
      tagline:     "Compares to a public fingerprint DB.",
      description: "Long-running research project (INRIA). Hashes your " +
                   "fingerprint and tells you how many other browsers in " +
                   "their dataset share the exact same signature. ~1-in-1 " +
                   "uniqueness is bad; ~1-in-1000 is the realistic ceiling. " +
                   "Useful as a sanity check, not a hard pass/fail.",
      checks:      ["UA", "Plugins", "Fonts", "Canvas", "WebGL",
                    "Headers", "Cookies enabled"],
      relevant:    ["ua", "plugins", "fonts", "canvas", "gpu"],
    },
    browserleaks: {
      icon:        "💧",
      label:       "BrowserLeaks",
      url:         "https://browserleaks.com/canvas",
      tagline:     "Per-API leak breakdown. Gold standard.",
      description: "Suite of one-page-per-API testers: canvas, WebRTC, " +
                   "fonts, geolocation, audio context, ClientRects, " +
                   "TLS-fingerprint (JA3). Doesn't aggregate into a " +
                   "single score -- you read each page individually. " +
                   "Best for hunting a specific leak you suspect.",
      checks:      ["Canvas", "WebRTC IP leak", "Fonts list",
                    "ClientRects", "Audio context", "JA3 TLS hash",
                    "Geolocation"],
      relevant:    ["critical", "canvas", "fonts", "audio", "webrtc"],
    },
    fpcom: {
      icon:        "🛡",
      label:       "Fingerprint.com BotD",
      url:         "https://fingerprint.com/products/bot-detection/",
      tagline:     "The realest test -- commercial bot-detect demo.",
      description: "Demo of the same engine many real sites pay to " +
                   "license. Returns 'Bot' vs 'Real Browser' verdict + " +
                   "tells you which technique caught you (UA mismatch, " +
                   "Selenium-driver leak, headless heuristics, " +
                   "stack-trace fingerprint, ...). If you pass this, " +
                   "you're statistically likely to pass commercial " +
                   "bot-walls in production.",
      checks:      ["Bot / Real Browser verdict", "Selenium / Playwright / Puppeteer detection",
                    "Headless heuristics", "Stack-trace fingerprint", "UA consistency"],
      relevant:    ["critical", "ua", "ua-ch"],
    },
  },

  _renderTesterCards() {
    const grid = document.getElementById("fp-tester-grid");
    if (!grid || grid.dataset._rendered === "1") return;
    grid.dataset._rendered = "1";
    grid.innerHTML = Object.entries(this.FP_TESTERS).map(([id, t]) => `
      <button type="button" class="fp-tester-card" data-tester-id="${id}"
              style="display:block; text-align:left; padding: 10px 12px;
                     border:1px solid var(--border,#2a3142);
                     border-radius: 8px; background: transparent;
                     color: inherit; cursor: pointer; transition: border-color .15s;">
        <div style="font-weight:600;">${t.icon} ${escapeHtml(t.label)}</div>
        <div class="muted" style="font-size:11px;">${escapeHtml(t.tagline)}</div>
        <div class="fp-tester-result" data-result-for="${id}"
             style="margin-top:6px; font-size:11px;"></div>
      </button>
    `).join("");
    grid.querySelectorAll("[data-tester-id]").forEach(btn => {
      btn.addEventListener("click", () =>
        this.openTesterModal(btn.dataset.testerId));
    });
    // Hover feedback so users learn these are clickable
    grid.querySelectorAll(".fp-tester-card").forEach(c => {
      c.addEventListener("mouseenter",
        () => c.style.borderColor = "var(--accent, #6366f1)");
      c.addEventListener("mouseleave",
        () => c.style.borderColor = "var(--border,#2a3142)");
    });
    // Show last results immediately on first render so users coming
    // back to the page see history without re-running the probe.
    this._loadAndRenderExternalFpResults();
  },

  // Fetch latest results once and paint each tester card.
  async _loadAndRenderExternalFpResults() {
    if (!this.currentProfile) return;
    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}` +
        `/fingerprint/external-results`);
      if (r && r.ok && r.latest) this._renderExternalFpResults(r.latest);
    } catch (e) {
      // Non-fatal: cards just stay blank. Don't toast here — users
      // shouldn't get a popup just because they opened the page.
    }
  },

  _renderExternalFpResults(latest) {
    const grid = document.getElementById("fp-tester-grid");
    if (!grid) return;
    grid.querySelectorAll("[data-result-for]").forEach(el => {
      const tid = el.dataset.resultFor;
      const r = latest[tid];
      if (!r) {
        el.innerHTML = `<span class="muted">no result yet — click 🚀 below</span>`;
        return;
      }
      const ts = r.timestamp ? timeAgo(r.timestamp) : "";
      if (r.error) {
        el.innerHTML =
          `<span style="color:#fca5a5;">✗ ${escapeHtml(r.error.slice(0,80))}</span>` +
          ` <span class="muted">${escapeHtml(ts)}</span>`;
        return;
      }
      const parts = [];
      if (typeof r.trust_score === "number") {
        const c = r.trust_score >= 70 ? "#6ee7b7"
                : r.trust_score >= 40 ? "#fcd34d"
                : "#fca5a5";
        parts.push(`<span style="color:${c}; font-weight:600;">` +
                   `trust ${r.trust_score}%</span>`);
      }
      if (typeof r.lies_count === "number" && r.lies_count > 0) {
        parts.push(`<span style="color:#fca5a5;">${r.lies_count} lies</span>`);
      }
      if (r.fingerprint_id) {
        parts.push(`<span class="muted" ` +
                   `style="font-family:ui-monospace,monospace;">` +
                   `${escapeHtml(r.fingerprint_id.slice(0,12))}</span>`);
      }
      if (!parts.length) parts.push(`<span class="muted">recorded</span>`);
      parts.push(`<span class="muted">${escapeHtml(ts)}</span>`);
      el.innerHTML = parts.join(" · ");
    });
  },

  // After a probe run is spawned, poll the external-results endpoint
  // every 8s until we see fresh results for all expected testers (or
  // we hit the timeout). Updates the per-tester result line in place.
  async _pollExternalFpResults(expectedCount, singleTesterId) {
    if (!this.currentProfile) return;
    const startTs = new Date().toISOString();   // anything newer than this is "fresh"
    const endAt   = Date.now() + 4 * 60 * 1000;  // up to 4 minutes
    const seen    = new Set();
    while (Date.now() < endAt) {
      await new Promise(r => setTimeout(r, 8000));
      try {
        const r = await api(
          `/api/profiles/${encodeURIComponent(this.currentProfile)}` +
          `/fingerprint/external-results`);
        if (r && r.ok && r.latest) {
          // Mark testers whose latest timestamp is newer than the
          // probe start as "seen this run".
          for (const [tid, row] of Object.entries(r.latest)) {
            if (row.timestamp && row.timestamp >= startTs) seen.add(tid);
          }
          this._renderExternalFpResults(r.latest);
          // If user picked a single tester, stop as soon as it reports.
          if (singleTesterId && seen.has(singleTesterId)) break;
          // Multi-tester probe: stop when we've seen all expected.
          if (!singleTesterId && seen.size >= expectedCount) break;
        }
      } catch (e) { /* keep polling */ }
    }
  },

  // Open the modal for one tester. Pulls coherence data from the
  // backend so we can show the profile's score for the signals
  // this particular tester checks.
  async openTesterModal(testerId) {
    const t = this.FP_TESTERS[testerId];
    if (!t) return;
    const modal  = document.getElementById("fp-tester-modal");
    const title  = document.getElementById("fp-tester-modal-title");
    const body   = document.getElementById("fp-tester-modal-body");
    const probe  = document.getElementById("fp-tester-modal-probe-btn");
    const link   = document.getElementById("fp-tester-modal-open-link");
    if (!modal || !title || !body) return;

    title.textContent = `${t.icon} ${t.label}`;
    link.href         = t.url;
    body.innerHTML    = `<div class="muted">Loading profile data…</div>`;
    modal.style.display = "";

    // Wire the probe button to spawn a single-tester probe run.
    probe.onclick = () => this._runFpProbe({ testerId });

    // Fetch coherence data for this profile -- gives us the per-test
    // breakdown we use to highlight which checks are relevant for
    // this specific tester.
    let cohData = null;
    try {
      cohData = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/fingerprint/coherence`);
    } catch (e) {
      cohData = { empty: true, message: "Could not fetch coherence data" };
    }

    body.innerHTML = this._renderTesterModalBody(t, cohData);
  },

  _renderTesterModalBody(t, cohData) {
    const checksHtml = `
      <div style="margin-top: 12px;">
        <div class="card-title" style="font-size: 12px; margin-bottom: 6px;">
          Signals this tester measures
        </div>
        <div style="display:flex; flex-wrap: wrap; gap: 6px;">
          ${t.checks.map(c =>
            `<span class="profile-tag-chip" style="font-size:11px;">${escapeHtml(c)}</span>`
          ).join("")}
        </div>
      </div>`;

    let cohHtml = "";
    if (cohData && cohData.empty) {
      cohHtml = `<div class="form-hint" style="margin-top: 14px;">
        ${escapeHtml(cohData.message ||
          "No coherence data yet. Launch the profile once to score the fingerprint.")}
      </div>`;
    } else if (cohData && cohData.results) {
      // Filter results to categories this tester actually cares about
      const relevant = cohData.results.filter(r =>
        t.relevant.some(rel =>
          (r.category || "").toLowerCase().includes(rel.toLowerCase())));
      const passed = relevant.filter(r => r.status === "pass").length;
      const total  = relevant.length;
      const score  = cohData.score != null ? `${cohData.score}/100` : "n/a";
      const grade  = cohData.grade || "—";

      const colors = {
        excellent: "var(--healthy, #22c55e)",
        good:      "var(--healthy, #22c55e)",
        warning:   "var(--warning, #f59e0b)",
        critical:  "var(--critical, #ef4444)",
      };
      const gradeColor = colors[grade] || "var(--text-muted)";

      cohHtml = `
        <div style="display:flex; gap: 14px; align-items: center;
                    margin-top: 14px; padding: 10px;
                    border: 1px solid var(--border,#2a3142);
                    border-radius: 8px;">
          <div>
            <div class="muted" style="font-size: 10px; text-transform: uppercase;">
              Internal coherence score
            </div>
            <div style="font-size: 22px; font-weight: 600; color: ${gradeColor};">
              ${score} <span style="font-size:12px; opacity:.7;">${escapeHtml(grade)}</span>
            </div>
          </div>
          <div style="flex: 1;">
            <div class="muted" style="font-size: 10px; text-transform: uppercase;">
              Relevant checks for this tester
            </div>
            <div style="font-size: 16px; font-weight: 500;">
              ${passed} / ${total} passing
            </div>
          </div>
        </div>`;

      if (relevant.length) {
        cohHtml += `
          <div style="margin-top: 12px;">
            <div class="card-title" style="font-size: 12px; margin-bottom: 6px;">
              Per-check breakdown (validator's view)
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr;
                        gap: 6px;">
              ${relevant.map(r => {
                const ico = r.status === "pass" ? "✓"
                          : r.status === "warn" ? "⚠"
                          : r.status === "fail" ? "✗" : "·";
                const cl  = r.status === "pass" ? "pass"
                          : r.status === "fail" ? "fail" : "warn";
                return `<div class="selfcheck-item ${cl}"
                             title="${escapeHtml(r.detail || '')}">
                  <span class="icon">${ico}</span>
                  <span>${escapeHtml(r.name)}</span>
                </div>`;
              }).join("")}
            </div>
          </div>`;
      } else {
        cohHtml += `<div class="form-hint" style="margin-top: 8px;">
          No internal validator checks map directly to this tester's signals --
          run the probe to see this tester's own verdict.
        </div>`;
      }
    }

    return `
      <div>${escapeHtml(t.description)}</div>
      ${checksHtml}
      ${cohHtml}
      <div class="form-hint" style="margin-top: 14px; padding: 8px;
           background: var(--surface-2, rgba(99,102,241,0.06));
           border-radius: 6px;">
        <strong>Note:</strong> external testers run JS in the browser they're
        visiting. To get THIS profile's verdict, click <em>Probe in profile</em>
        below -- a probe run will open ${escapeHtml(t.label)} in the profile's
        actual Chrome session and you'll see the on-page score there. Opening
        the link in a new tab uses your dashboard browser, NOT this profile.
      </div>`;
  },

  async _runFpProbe(opts = {}) {
    // Two callsites:
    //   1. The big "Probe in profile" button at the bottom of the
    //      Self-Check card — visits all 6 testers (opts.testerId
    //      is undefined).
    //   2. The "Probe in profile" button INSIDE each tester modal —
    //      visits just that one tester (opts.testerId is set).
    const btn    = document.getElementById("fp-probe-btn");
    const status = document.getElementById("fp-probe-status");
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    const single = !!opts.testerId;
    const t      = single ? (this.FP_TESTERS || {})[opts.testerId] : null;
    const label  = single && t ? t.label : "all 6 testers";
    const dur    = single ? "~30-60s" : "~3-4 minutes";

    const ok = await confirmDialog({
      title:        "Run fingerprint probe pass?",
      message:      `This will launch ${this.currentProfile} and visit ` +
                    `${label}. Takes ${dur}. Watch the Logs page for progress; ` +
                    `tester scores will be visible in the Chrome window ` +
                    `that opens.`,
      confirmText:  "Start probe",
      confirmStyle: "primary",
    });
    if (!ok) return;

    if (btn) {
      btn.disabled = true;
      btn.dataset._origText = btn.textContent;
      btn.textContent = "⏳ Spawning…";
    }
    if (status) status.textContent = "";
    try {
      const body = single ? { tester_id: opts.testerId } : {};
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/fingerprint/probe`,
        { method: "POST", body: JSON.stringify(body) },
      );
      if (r.ok === false) {
        toast(`Probe failed: ${r.error || "unknown"}`, true);
        if (status) status.textContent = `✗ ${r.error || "failed"}`;
      } else {
        toast(`✓ Probe run #${r.run_id} started — results in ~${r.tester_count*30}s`);
        if (status) {
          status.textContent =
            `Run #${r.run_id} started — visiting ${r.tester_count} tester(s) ` +
            `(results will appear under each tester card when ready)`;
          status.style.color = "#6ee7b7";
        }
        // Auto-close the per-tester modal so user sees the toast +
        // Logs page suggestion uncluttered.
        if (single) {
          const m = document.getElementById("fp-tester-modal");
          if (m) m.style.display = "none";
        }
        // Start polling for results — each tester takes ~30-40s
        // including dwell + extract, so we poll every 8s for up to
        // 4 minutes and stop early once all expected testers report.
        this._pollExternalFpResults(r.tester_count || 1, opts.testerId);
      }
    } catch (e) {
      toast(`Probe failed: ${e.message || e}`, true);
      if (status) status.textContent = `✗ ${e.message || e}`;
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = btn.dataset._origText || "🚀 Probe in profile";
      }
    }
  },

  async loadFingerprint(name) {
    // Dual-purpose: fill the raw-JSON debug view AND populate the
    // new coherence-score card. Uses the new /api/fingerprint/<name>
    // endpoint which returns the FULL DB row (payload + coherence_report)
    // rather than the legacy shape which is just the payload dict.
    try {
      const resp = await api(`/api/fingerprint/${encodeURIComponent(name)}`);
      const fp = resp.fingerprint;     // row or null
      this._renderFingerprintCard(fp);

      // Legacy debug view — show the payload so existing users who relied
      // on the pretty-printed JSON still see their data.
      if ($("#fingerprint-view")) {
        if (fp) {
          $("#fingerprint-view").innerHTML = fmtJson(fp.payload || fp);
        } else {
          $("#fingerprint-view").innerHTML = '<span class="muted">No fingerprint generated yet.</span>';
        }
      }
    } catch (e) {
      this._renderFingerprintCard(null, e.message);
      if ($("#fingerprint-view")) {
        $("#fingerprint-view").innerHTML = `<span class="muted">${escapeHtml(e.message)}</span>`;
      }
    }
  },

  // ─────────────────────────────────────────────────────────────
  // Fingerprint mini-card — visible on profile detail page.
  // Feeds from the same /api/fingerprint/<name> payload as the
  // full editor page, but shows only score + template + CTAs.
  // ─────────────────────────────────────────────────────────────
  _renderFingerprintCard(fp, errMsg) {
    const badge    = document.getElementById("profile-fp-badge");
    const scoreEl  = document.getElementById("profile-fp-score");
    const gradeEl  = document.getElementById("profile-fp-grade");
    const sumEl    = document.getElementById("profile-fp-summary");
    const metaEl   = document.getElementById("profile-fp-meta");
    const openBtn  = document.getElementById("profile-fp-open-btn");
    if (!badge) return;    // profile.html snippet not present (old cached copy)

    // Reset colour classes so previous profile's grade doesn't stick
    badge.className = "profile-fp-mini-badge fp-score-unknown";

    // Always pass current profile name into the editor link so the
    // editor can preselect it via its ?profile=... hash parser.
    if (openBtn && this.currentProfile) {
      openBtn.setAttribute("data-nav-params",
        `profile=${encodeURIComponent(this.currentProfile)}`);
      // Also patch the hash on click — data-nav navigates without params
      // so we wrap the click to append ?profile=… before nav fires.
      openBtn.onclick = (e) => {
        e.preventDefault();
        location.hash = `#fingerprint?profile=${encodeURIComponent(this.currentProfile)}`;
        navigate("fingerprint");
      };
    }

    if (errMsg) {
      scoreEl.textContent = "!";
      gradeEl.textContent = "error";
      sumEl.textContent = errMsg;
      metaEl.textContent = "";
      return;
    }

    if (!fp) {
      scoreEl.textContent = "—";
      gradeEl.textContent = "no data";
      sumEl.textContent = "No fingerprint generated yet. Open the editor to create one.";
      metaEl.textContent = "";
      return;
    }

    const rep   = fp.coherence_report || {};
    const score = fp.coherence_score ?? rep.score;
    const grade = rep.grade || "unknown";

    badge.classList.remove("fp-score-unknown");
    badge.classList.add(`fp-score-${grade}`);
    scoreEl.textContent = score == null ? "—" : score;
    gradeEl.textContent = grade;

    sumEl.textContent = rep.summary
      || (fp.template_name || fp.template_id || "unknown template");

    // Per-domain breakdown badges (Identity / Hardware / Network /
    // Automation). Surfaces which dimension is weak instead of just
    // an aggregate score. Hidden when validator didn't return
    // by_domain (older snapshots predating the catalog refactor).
    const domainsBox = document.getElementById("profile-fp-domains");
    if (domainsBox) {
      const byDomain = rep.by_domain || null;
      if (byDomain && Object.keys(byDomain).length > 0) {
        const ICONS = {
          identity:   "🪪",
          hardware:   "🖥",
          network:    "🌐",
          automation: "🤖",
        };
        const ORDER = ["identity", "hardware", "network", "automation"];
        const sortedKeys = ORDER.filter(k => k in byDomain).concat(
          Object.keys(byDomain).filter(k => !ORDER.includes(k))
        );
        domainsBox.innerHTML = sortedKeys.map(d => {
          const v = byDomain[d];
          const dg = v.grade || "unknown";
          const failBit = v.fail
            ? `<span class="fp-domain-fail">${v.fail} fail</span>`
            : "";
          const warnBit = v.warn
            ? `<span class="fp-domain-warn">${v.warn} warn</span>`
            : "";
          return `
            <div class="fp-domain-pill fp-score-${dg}" title="${v.pass} pass · ${v.warn} warn · ${v.fail} fail · ${v.skip} skip">
              <span class="fp-domain-icon">${ICONS[d] || "•"}</span>
              <span class="fp-domain-label">${d}</span>
              <span class="fp-domain-score">${v.score}</span>
              ${failBit}${warnBit}
            </div>`;
        }).join("");
        domainsBox.style.display = "";
      } else {
        domainsBox.innerHTML = "";
        domainsBox.style.display = "none";
      }
    }

    const parts = [];
    if (fp.template_name || fp.template_id) {
      parts.push(`📦 ${fp.template_name || fp.template_id}`);
    }
    if (fp.source) parts.push(`source: ${fp.source}`);
    if (fp.timestamp) parts.push(`generated ${timeAgo(fp.timestamp)}`);
    metaEl.textContent = parts.join(" · ");
  },

  // Quick regenerate — reuses the library /generate endpoint with
  // mode=full so the same codepath as the full editor kicks in.
  async quickRegenerateFingerprint() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Quick regenerate?",
      message: `Generate a fresh fingerprint for "${this.currentProfile}"? The current one moves to history — you can restore it from the Fingerprint editor.`,
      confirmText: "Regenerate",
    })) return;
    try {
      const resp = await api(
        `/api/fingerprint/${encodeURIComponent(this.currentProfile)}/generate`,
        { method: "POST",
          body: JSON.stringify({ mode: "full", reason: "quick regenerate from profile page" }) }
      );
      toast(`✓ Regenerated · score ${resp.validation?.score ?? "—"}/100`);
      await this.loadFingerprint(this.currentProfile);
    } catch (e) {
      toast("Regenerate failed: " + e.message, true);
    }
  },

  // ─────────────────────────────────────────────────────────────
  // Session summary mini-card (profile page). Mirrors the fingerprint
  // card pattern — pulls /api/session/<name> and populates the card
  // without fetching the full warmup history or snapshots (those live
  // on the dedicated Session page).
  // ─────────────────────────────────────────────────────────────
  async loadSessionSummary(name) {
    const countEl  = document.getElementById("profile-session-count");
    const sumEl    = document.getElementById("profile-session-summary");
    const metaEl   = document.getElementById("profile-session-meta");
    const openBtn  = document.getElementById("profile-session-open-btn");
    if (!countEl || !name) return;

    // Patch the editor link to pre-select this profile (same pattern as
    // the fingerprint card does) — fingerprint-page listener reads the
    // ?profile=... hash on init.
    if (openBtn) {
      openBtn.onclick = (e) => {
        e.preventDefault();
        location.hash = `#session?profile=${encodeURIComponent(name)}`;
        navigate("session");
      };
    }
    // Wire cookie pool button (idempotent)
    this._wireCookiePoolBtn();

    try {
      const r = await api(`/api/session/${encodeURIComponent(name)}`);
      const last  = r?.warmup?.last;
      const stats = r?.snapshots || {};
      const running = r?.warmup?.running;

      countEl.textContent = stats.n ?? "—";

      const parts = [];
      if (running) {
        sumEl.textContent = "Warmup running…";
      } else if (last) {
        sumEl.textContent =
          `Last warmup ${timeAgo(last.started_at)} · ${last.preset} · ${last.status}`;
        parts.push(`${last.sites_succeeded}/${last.sites_planned} sites ok`);
      } else {
        sumEl.textContent = "No warmup yet — open session manager to run one.";
      }
      if (stats.total_cookies) parts.push(`${stats.total_cookies} cookies in pool`);
      if (stats.last_at)       parts.push(`snapshot ${timeAgo(stats.last_at)}`);
      metaEl.textContent = parts.join(" · ");
    } catch (e) {
      sumEl.textContent = "Failed to load session status: " + e.message;
      metaEl.textContent = "";
    }
  },

  async resetHealth() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Reset health counter",
      message: `Reset consecutive blocks counter for "${this.currentProfile}"?`,
      confirmText: "Reset",
    })) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/reset-health`,
                { method: "POST" });
      toast("✓ Blocks counter reset");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async clearHistory() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Clear history",
      message: `Clear ALL session quality history for "${this.currentProfile}"?\nThis cannot be undone.`,
      confirmText: "Clear",
      confirmStyle: "warning",
    })) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/clear-history`,
                { method: "POST" });
      toast("✓ History cleared");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async deleteProfile() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Delete profile",
      message:
        `Delete profile "${this.currentProfile}"?\n\n` +
        `This removes the profile folder AND purges all related DB rows ` +
        `(events, fingerprints, self-checks, tags, notes). Run history ` +
        `is kept for historical stats but the profile will no longer ` +
        `appear in dropdowns.\n\n` +
        `If this is the currently-active profile, it will be reassigned ` +
        `to the next available one automatically.\n\n` +
        `This cannot be undone.`,
      confirmText: "Delete profile",
      confirmStyle: "danger",
    })) return;

    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}`,
        { method: "DELETE" }
      );
      // Reload the global config cache so other pages see the new
      // `browser.profile_name` value. Without this, the sidebar badge +
      // Overview "Profile X active" stay pointing at the deleted one.
      await loadConfig();
      if (r.reassigned_to) {
        toast(
          `✓ Deleted "${this.currentProfile}". ` +
          `Active profile reassigned to "${r.reassigned_to}".`
        );
      } else {
        toast(`✓ Deleted "${this.currentProfile}"`);
      }
      navigate("profiles");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  // ═══════════════════════════════════════════════════════════
  // Sprint 8: Backup / Restore UI
  // ═══════════════════════════════════════════════════════════
  // Bundle format and crypto live in ghost_shell/profile/backup.py.
  // This UI is a thin wrapper around the three backup endpoints:
  //
  //   POST /api/profiles/<name>/backup    → encrypted blob (binary)
  //   POST /api/backup/inspect            → header preview (json)
  //   POST /api/backup/restore            → multipart upload
  //
  // We never persist the master password — it's read from the DOM
  // input on submit, posted, then the input is wiped on modal close.

  /** Open the download-backup modal. Just shows the panel and
   *  resets transient state — no network call yet. */
  _openBackupModal() {
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    this._resetBackupModal();
    const m = document.getElementById("profile-backup-modal");
    if (m) m.style.display = "flex";
    // Focus the password field so the user can type immediately.
    setTimeout(() => {
      document.getElementById("profile-backup-password")?.focus();
    }, 50);
  },

  /** Wipe modal state — password input cleared, status reset. Called
   *  on open and on close so a previous attempt's password doesn't
   *  leak into the next session. */
  _resetBackupModal() {
    const pw = document.getElementById("profile-backup-password");
    const st = document.getElementById("profile-backup-modal-status");
    const sb = document.getElementById("profile-backup-submit");
    if (pw) pw.value = "";
    if (st) { st.textContent = ""; st.className = "muted"; }
    if (sb) {
      sb.disabled = false;
      sb.textContent = "🔐 Encrypt & download";
      // UX-03 (sprint-8-audit): _submitCloudPush swaps sb.onclick to
      // _cloudPushFromModal. Clear that on every reset so the next
      // open of this same modal goes through the default
      // _submitBackup wired in init(), not the cloud-push handler.
      sb.onclick = null;
    }
  },

  /** Submit the backup-create flow.
   *
   *  We can't use the json-only `api()` helper because the response
   *  is binary (Content-Type: application/octet-stream). Instead we
   *  use raw fetch + Blob.
   *
   *  On success: trigger a hidden `<a download>` click so the
   *  browser saves the file. On failure: show the server's error
   *  message in the modal status line. */
  async _submitBackup() {
    const pw = document.getElementById("profile-backup-password");
    const st = document.getElementById("profile-backup-modal-status");
    const sb = document.getElementById("profile-backup-submit");
    const password = (pw?.value || "").trim();
    if (!password) {
      if (st) {
        st.textContent = "Master password is required.";
        st.className = "muted profile-proxy-test-fail";
      }
      pw?.focus();
      return;
    }
    if (st) {
      st.textContent = "Encrypting…";
      st.className = "muted";
    }
    if (sb) { sb.disabled = true; sb.textContent = "Encrypting…"; }

    try {
      const url = `/api/profiles/${encodeURIComponent(this.currentProfile)}/backup`;
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ master_password: password }),
      });
      // Server returns JSON on error, binary on success.
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const j = await r.json();
          msg = j.error || msg;
        } catch (_) {}
        throw new Error(msg);
      }
      const blob = await r.blob();
      // Try to honor Content-Disposition's filename. Fall back to
      // `<profile>_<ts>.ghs-bundle` when the header is missing
      // (some proxies strip Content-Disposition).
      let fname = `${this.currentProfile}_${Date.now()}.ghs-bundle`;
      const cd = r.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^"]+)"?/i);
      if (m) fname = m[1];

      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke the object URL after the browser has had time to
      // pick up the blob — safari/firefox have raced on this.
      setTimeout(() => URL.revokeObjectURL(blobUrl), 5000);

      if (st) {
        st.textContent = `✓ Saved ${fname} · ${(blob.size / 1024).toFixed(1)} KB`;
        st.className = "muted profile-proxy-test-ok";
      }
      toast(`✓ Backup saved (${(blob.size / 1024).toFixed(1)} KB)`);
      // Wipe the password and re-enable the button so the user can
      // download again with a different password without reopening.
      if (pw) pw.value = "";
      if (sb) { sb.disabled = false; sb.textContent = "🔐 Encrypt & download"; }
      // Footer sticky status — survives the modal close.
      const fst = document.getElementById("profile-backup-status");
      if (fst) fst.textContent = `Last backup: ${fname}`;
    } catch (e) {
      if (st) {
        st.textContent = `✗ ${e.message}`;
        st.className = "muted profile-proxy-test-fail";
      }
      if (sb) { sb.disabled = false; sb.textContent = "🔐 Encrypt & download"; }
    }
  },

  /** Open the restore-from-file modal. Resets to step 1 (file
   *  picker) — even if the modal was previously left on step 2 —
   *  so the user always starts from a clean slate. */
  _openRestoreModal() {
    this._resetRestoreModal();
    const m = document.getElementById("profile-restore-modal");
    if (m) m.style.display = "flex";
  },

  /** Full reset of the restore modal — used on open and on close.
   *  Wipes file selection, password, target name, overwrite flag,
   *  and snaps the UI back to step 1. */
  _resetRestoreModal() {
    this._restoreInspected = null;
    this._restoreFile      = null;
    const fileInput = document.getElementById("profile-restore-file");
    if (fileInput) fileInput.value = "";
    const tn = document.getElementById("profile-restore-target-name");
    if (tn) tn.value = "";
    const ow = document.getElementById("profile-restore-overwrite");
    if (ow) ow.checked = false;
    const pw = document.getElementById("profile-restore-password");
    if (pw) pw.value = "";
    const ps = document.getElementById("profile-restore-pick-status");
    if (ps) { ps.textContent = ""; ps.className = "muted"; }
    const cs = document.getElementById("profile-restore-confirm-status");
    if (cs) { cs.textContent = ""; cs.className = "muted"; }
    this._restoreBackToPick();
    const ib = document.getElementById("profile-restore-inspect-btn");
    if (ib) ib.disabled = true;
  },

  /** Step navigation: pick (step 1) → confirm (step 2). */
  _restoreShowConfirm() {
    document.getElementById("profile-restore-step-pick").style.display = "none";
    document.getElementById("profile-restore-step-confirm").style.display = "";
    document.getElementById("profile-restore-inspect-btn").style.display = "none";
    document.getElementById("profile-restore-submit-btn").style.display = "";
    document.getElementById("profile-restore-back-btn").style.display = "";
    setTimeout(() => {
      document.getElementById("profile-restore-password")?.focus();
    }, 50);
  },

  _restoreBackToPick() {
    const a = document.getElementById("profile-restore-step-pick");
    const b = document.getElementById("profile-restore-step-confirm");
    const ib = document.getElementById("profile-restore-inspect-btn");
    const sb = document.getElementById("profile-restore-submit-btn");
    const back = document.getElementById("profile-restore-back-btn");
    if (a) a.style.display = "";
    if (b) b.style.display = "none";
    if (ib) ib.style.display = "";
    if (sb) sb.style.display = "none";
    if (back) back.style.display = "none";
  },

  /** File picker change handler. Stashes the File object and
   *  enables the inspect button. We don't read the file yet — the
   *  inspect step does that to keep the size of the JS heap small
   *  for huge bundles. */
  _onRestoreFilePicked(file) {
    const ib = document.getElementById("profile-restore-inspect-btn");
    const ps = document.getElementById("profile-restore-pick-status");
    if (!file) {
      this._restoreFile = null;
      if (ib) ib.disabled = true;
      if (ps) { ps.textContent = ""; ps.className = "muted"; }
      return;
    }
    this._restoreFile = file;
    if (ib) ib.disabled = false;
    if (ps) {
      ps.textContent = `Selected ${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
      ps.className = "muted";
    }
  },

  /** POST the file to /api/backup/inspect — header-only preview, no
   *  password required. On success, render the metadata in step 2
   *  and advance the modal. On failure (bad magic / version), stay
   *  on step 1 with an error message. */
  async _submitInspect() {
    const file = this._restoreFile;
    const ps = document.getElementById("profile-restore-pick-status");
    if (!file) {
      if (ps) {
        ps.textContent = "Pick a file first.";
        ps.className = "muted profile-proxy-test-fail";
      }
      return;
    }
    if (ps) { ps.textContent = "Reading header…"; ps.className = "muted"; }

    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const r = await fetch("/api/backup/inspect", {
        method: "POST",
        body: fd,
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      this._restoreInspected = j.info;
      this._renderRestorePreview(j.info, file);
      this._restoreShowConfirm();
    } catch (e) {
      if (ps) {
        ps.textContent = `✗ ${e.message}`;
        ps.className = "muted profile-proxy-test-fail";
      }
    }
  },

  /** Format the inspect-bundle response into human-readable copy.
   *  The server only exposes the unencrypted header here (magic,
   *  version, salt, payload size, total size). We deliberately do
   *  NOT show the salt — it's not secret, but it adds noise. */
  _renderRestorePreview(info, file) {
    const el = document.getElementById("profile-restore-preview");
    if (!el) return;
    const totalKb = (info.total_size_bytes / 1024).toFixed(1);
    const payloadKb = (info.payload_size / 1024).toFixed(1);
    el.innerHTML = `
      <div><strong>${escapeHtml(file.name)}</strong></div>
      <div>Magic: <code>${escapeHtml(info.magic)}</code> · Version: v${info.version}</div>
      <div>Total size: ${totalKb} KB · Encrypted payload: ${payloadKb} KB</div>
      <div style="margin-top: 6px; font-style: italic;">
        The bundle's profile name and source host are inside the
        encrypted payload — they show up after a successful decrypt.
      </div>
    `.trim();
  },

  /** Final step — POST the bundle, password, target name, and
   *  overwrite flag to /api/backup/restore. The endpoint returns
   *  401 on auth failure, 400 on format error, 409 on collision,
   *  and 200 on success with a written-rows summary. */
  async _submitRestore() {
    const file = this._restoreFile;
    const cs = document.getElementById("profile-restore-confirm-status");
    const sb = document.getElementById("profile-restore-submit-btn");
    if (!file) {
      if (cs) {
        cs.textContent = "No file selected — go back.";
        cs.className = "muted profile-proxy-test-fail";
      }
      return;
    }
    const pw = (document.getElementById("profile-restore-password")?.value || "").trim();
    if (!pw) {
      if (cs) {
        cs.textContent = "Master password is required.";
        cs.className = "muted profile-proxy-test-fail";
      }
      return;
    }
    const target = (document.getElementById("profile-restore-target-name")?.value || "").trim();
    const overwrite = !!document.getElementById("profile-restore-overwrite")?.checked;

    if (cs) { cs.textContent = "Decrypting & restoring…"; cs.className = "muted"; }
    if (sb) { sb.disabled = true; sb.textContent = "Restoring…"; }

    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("master_password", pw);
      if (target) fd.append("target_profile_name", target);
      fd.append("overwrite", overwrite ? "true" : "false");
      const r = await fetch("/api/backup/restore", {
        method: "POST",
        body: fd,
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        // Friendly mapping per server's category code
        const cat = j.category || "";
        const baseMsg = j.error || `HTTP ${r.status}`;
        if (cat === "auth") {
          throw new Error("Wrong master password — bundle could not be decrypted.");
        }
        if (cat === "format") {
          throw new Error("Bundle format error — file is corrupt or from a future Ghost Shell.");
        }
        if (cat === "conflict") {
          throw new Error(baseMsg + " (tick 'Overwrite' or pick a different name).");
        }
        throw new Error(baseMsg);
      }

      // Success path — show a summary and offer to navigate to the
      // restored profile. We don't auto-navigate because the user
      // may want to back-up the just-restored profile too.
      const written = j.written || {};
      const rows = Object.entries(written)
        .map(([t, n]) => `${t}: ${n}`).join(", ");
      const restoredName = j.target_name || "?";
      const sourceHost   = j.source_host || "?";
      const udExtracted  = j.user_data_extracted || 0;
      const warnHtml = (j.warnings && j.warnings.length)
        ? `<div style="margin-top:8px; color:#e2b860;">Warnings: ${j.warnings
              .map(w => escapeHtml(w)).join("; ")}</div>`
        : "";
      if (cs) {
        cs.innerHTML = `
          ✓ Restored as <strong>${escapeHtml(restoredName)}</strong>
          (from <code>${escapeHtml(sourceHost)}</code>) ·
          ${rows} · ${udExtracted} user-data files extracted.
          ${warnHtml}
          <div style="margin-top:8px;">
            <a href="#profile?name=${encodeURIComponent(restoredName)}"
               class="inline-link"
               data-nav="profile"
               data-nav-arg="${escapeHtml(restoredName)}">
              Open restored profile →
            </a>
          </div>
        `.trim();
        cs.className = "muted profile-proxy-test-ok";
      }
      toast(`✓ Restored "${restoredName}"`);
      if (sb) { sb.disabled = false; sb.textContent = "🔐 Decrypt & restore"; }
      // Wipe just the password so a second click is safe — leave
      // file + target name + overwrite alone in case the user wants
      // to re-run with a tweak.
      const pwEl = document.getElementById("profile-restore-password");
      if (pwEl) pwEl.value = "";
      // If we restored the currently-open profile, refresh the
      // page so the freshly-imported rows show up.
      if (this.currentProfile && restoredName === this.currentProfile) {
        setTimeout(() => location.reload(), 1500);
      }
    } catch (e) {
      if (cs) {
        cs.textContent = `✗ ${e.message}`;
        cs.className = "muted profile-proxy-test-fail";
      }
      if (sb) { sb.disabled = false; sb.textContent = "🔐 Decrypt & restore"; }
    }
  },

  // ═══════════════════════════════════════════════════════════
  // Sprint 8.2: Cloud sync (push / restore-from-cloud)
  // ═══════════════════════════════════════════════════════════
  // The dashboard never sees provider credentials — it just calls
  // the four /api/backup/sync/* endpoints which delegate to the
  // backup_sync.py adapters. Master password is collected fresh
  // for every push (we won't reuse it across calls).

  /** Probe the configured target. Reveal the cloud row only on
   *  ok=true — anything else stays hidden so unconfigured installs
   *  don't see broken buttons. Errors are silent (debug log only). */
  async _probeCloudSync() {
    const row = document.getElementById("profile-backup-cloud-row");
    if (!row) return;
    try {
      const r = await fetch("/api/backup/sync/test", { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) {
        row.style.display = "";
        const badge = document.getElementById("profile-backup-cloud-provider");
        if (badge) badge.textContent = (j.provider || "?").toUpperCase();
      }
    } catch (_) {
      // Silent — no cloud config = no UI.
    }
  },

  /** Push a fresh bundle to the configured cloud target. Reuses the
   *  master-password modal — same UX as local download but the
   *  resulting bytes never touch the user's filesystem. */
  async _submitCloudPush() {
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    // Open the existing backup-password modal but swap the submit
    // handler so it pushes to cloud instead of triggering a download.
    this._resetBackupModal();
    const m = document.getElementById("profile-backup-modal");
    if (m) m.style.display = "flex";
    const sb = document.getElementById("profile-backup-submit");
    if (sb) {
      sb.textContent = "☁ Encrypt & push";
      // Replace the click handler for this one open. We restore the
      // download handler via _resetBackupModal on close.
      sb.onclick = () => this._cloudPushFromModal();
    }
    setTimeout(() => {
      document.getElementById("profile-backup-password")?.focus();
    }, 50);
  },

  /** Inner submit handler for cloud-push when the password modal
   *  is open in cloud-push mode. */
  async _cloudPushFromModal() {
    const pw = document.getElementById("profile-backup-password");
    const st = document.getElementById("profile-backup-modal-status");
    const sb = document.getElementById("profile-backup-submit");
    const password = (pw?.value || "").trim();
    if (!password) {
      if (st) {
        st.textContent = "Master password is required.";
        st.className = "muted profile-proxy-test-fail";
      }
      pw?.focus();
      return;
    }
    if (st) { st.textContent = "Encrypting & uploading…"; st.className = "muted"; }
    if (sb) { sb.disabled = true; sb.textContent = "Uploading…"; }
    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/backup/sync/push`,
        { method: "POST", body: JSON.stringify({ master_password: password }) }
      );
      const sizeKb = ((r.size || 0) / 1024).toFixed(1);
      const deletedN = (r.deleted || []).length;
      const retentionMsg = deletedN
        ? ` · retention: removed ${deletedN} older bundle${deletedN === 1 ? "" : "s"}`
        : "";
      if (st) {
        st.innerHTML = `✓ Uploaded <code>${escapeHtml(r.key || "?")}</code>` +
                       ` · ${sizeKb} KB${retentionMsg}`;
        st.className = "muted profile-proxy-test-ok";
      }
      const fst = document.getElementById("profile-backup-cloud-status");
      if (fst) fst.textContent = `Last push: ${sizeKb} KB${retentionMsg}`;
      toast(`✓ Pushed to cloud (${sizeKb} KB)${retentionMsg}`);
      if (pw) pw.value = "";
      if (sb) {
        sb.disabled = false;
        sb.textContent = "☁ Encrypt & push";
      }
    } catch (e) {
      if (st) {
        st.textContent = `✗ ${e.message}`;
        st.className = "muted profile-proxy-test-fail";
      }
      if (sb) {
        sb.disabled = false;
        sb.textContent = "☁ Encrypt & push";
      }
    }
  },

  /** Open the cloud-restore picker — fetches the bundle list and
   *  renders one row per remote bundle. Click → download the bytes
   *  and feed them into the existing local-restore flow (header
   *  inspect → password input → restore). */
  async _openCloudRestoreModal() {
    const m = document.getElementById("profile-cloud-restore-modal");
    const body = document.getElementById("profile-cloud-restore-body");
    if (!m || !body) return;
    m.style.display = "flex";
    body.innerHTML = `<div class="muted" style="padding: 12px;">Loading remote bundles…</div>`;
    try {
      const r = await api("/api/backup/sync/list");
      const items = r.items || [];
      if (!items.length) {
        body.innerHTML = `
          <div class="dense-empty" style="padding: 30px 16px; text-align: center;">
            <div style="font-size: 28px; opacity: 0.4; margin-bottom: 8px;">☁</div>
            <div style="font-size: 13px; opacity: 0.75;">
              No bundles in the cloud target yet. Push one first
              with <strong>☁ Push to cloud</strong>.
            </div>
          </div>`;
        return;
      }
      // Group by host then profile. Most-recent first within each
      // group (the server already sorts ts desc).
      const byHostProfile = {};
      for (const it of items) {
        const k = `${it.host || "?"}/${it.profile_name || "?"}`;
        (byHostProfile[k] = byHostProfile[k] || []).push(it);
      }
      const groups = Object.keys(byHostProfile).sort();
      const html = groups.map(g => {
        const rows = byHostProfile[g];
        const first = rows[0];
        const inner = rows.map(it => {
          const sizeKb = ((it.size || 0) / 1024).toFixed(1);
          return `
            <tr>
              <td><code>${escapeHtml(it.stamp || it.key)}</code></td>
              <td class="num">${sizeKb} KB</td>
              <td>
                <button class="btn btn-primary btn-small profile-cloud-fetch-btn"
                        data-key="${escapeHtml(it.key)}">
                  ⬇ Fetch &amp; restore
                </button>
                <button class="btn btn-secondary btn-small profile-cloud-delete-btn"
                        data-key="${escapeHtml(it.key)}"
                        title="Remove this bundle from the cloud target">
                  🗑
                </button>
              </td>
            </tr>`;
        }).join("");
        return `
          <div style="margin-bottom: 14px;">
            <div style="font-weight: 600; margin-bottom: 4px;">
              ${escapeHtml(first.host)} / ${escapeHtml(first.profile_name)}
              <span class="muted" style="font-weight: normal; font-size: 12px;">
                — ${rows.length} bundle${rows.length === 1 ? "" : "s"}
              </span>
            </div>
            <table class="dense-table" style="width: 100%;">
              <thead>
                <tr>
                  <th>Timestamp</th>
                  <th class="num">Size</th>
                  <th style="width: 220px;">Actions</th>
                </tr>
              </thead>
              <tbody>${inner}</tbody>
            </table>
          </div>`;
      }).join("");
      body.innerHTML = html;
      body.querySelectorAll(".profile-cloud-fetch-btn").forEach(btn => {
        btn.addEventListener("click", () =>
          this._cloudFetchAndRestore(btn.dataset.key));
      });
      body.querySelectorAll(".profile-cloud-delete-btn").forEach(btn => {
        btn.addEventListener("click", () =>
          this._cloudDelete(btn.dataset.key));
      });
    } catch (e) {
      body.innerHTML = `<div class="muted" style="color:#fca5a5; padding:12px;">
        Load failed: ${escapeHtml(e.message)}
      </div>`;
    }
  },

  /** Pull the bytes for a remote key, hand them to the local
   *  restore flow as if the user had picked a file. We craft a
   *  pseudo-File object so _onRestoreFilePicked +_submitInspect work
   *  unchanged — the rest of the flow has no idea the bytes came
   *  from S3 / Dropbox / SFTP. */
  async _cloudFetchAndRestore(key) {
    if (!key) return;
    toast(`Fetching ${key}…`);
    try {
      const r = await fetch("/api/backup/sync/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      const blob = await r.blob();
      // Flask streamed the bundle; wrap it in a File so the local
      // flow doesn't need a special branch.
      const fname = key.split("/").pop() || "remote.ghs-bundle";
      const file = new File([blob], fname, {
        type: "application/octet-stream",
      });
      // Close the cloud picker, open the local-restore modal pre-
      // populated with the fetched bytes.
      const cloudModal = document.getElementById("profile-cloud-restore-modal");
      if (cloudModal) cloudModal.style.display = "none";
      this._openRestoreModal();
      this._onRestoreFilePicked(file);
      // Auto-advance to the inspect step — no point making the user
      // click "Inspect bundle" when they already chose this file.
      this._submitInspect();
    } catch (e) {
      toast(`Fetch failed: ${e.message}`, true);
    }
  },

  /** Hard-delete a remote bundle. Confirmation prompt because there's
   *  no recycle bin on the cloud side. */
  async _cloudDelete(key) {
    if (!key) return;
    if (!await confirmDialog({
      title: "Delete remote bundle?",
      message: `Permanently delete the cloud bundle:\n\n${key}\n\n` +
               `This removes it from the configured remote target. ` +
               `Local copies are untouched. Cannot be undone.`,
      confirmText: "Delete",
      confirmStyle: "danger",
    })) return;
    try {
      await api("/api/backup/sync/delete", {
        method: "POST",
        body: JSON.stringify({ key }),
      });
      toast(`✓ Deleted ${key}`);
      // Refresh the modal listing so the row disappears.
      this._openCloudRestoreModal();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, true);
    }
  },


  async regenerateFingerprint() {
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    if (!await confirmDialog({
      title: "🎲 Regenerate fingerprint?",
      message: `The fingerprint for "${this.currentProfile}" will be ` +
        `replaced with a freshly-generated one (new UA, screen, GPU, fonts, etc.). ` +
        `The self-check cache will be cleared. The profile's user-data-dir ` +
        `(cookies, history) is NOT touched.\n\n` +
        `Use this when the current fingerprint is getting flagged.`,
      confirmText: "Regenerate",
      confirmStyle: "primary",
    })) return;

    const btn = document.getElementById("regen-fp-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ Rolling…";
    }

    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}`
        + `/regenerate-fingerprint`,
        { method: "POST", body: JSON.stringify({}) }
      );
      if (r.ok) {
        toast(`✓ New fingerprint: ${r.template} (Chrome ${r.chrome_version})`);
        await this.loadFingerprint(this.currentProfile);
      } else {
        toast(r.error || "regeneration failed", true);
      }
    } catch (e) {
      toast(e.message || "regeneration failed", true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🎲 Regenerate fingerprint";
      }
    }
  },

  // ─── CHROME HISTORY IMPORT ───────────────────────────────────
  //
  // Populates the source-path input with whatever discover_source()
  // found on this machine. Shows the full candidate list in the hint
  // so users on multi-Chrome setups (work + personal) can see where
  // else to point the field.

  async _populateChromeImportSource() {
    const input = document.getElementById("chrome-import-source");
    const hint  = document.getElementById("chrome-import-source-hint");
    if (!input) return;
    try {
      const r = await api("/api/chrome-import/discover");
      if (r.source) {
        input.placeholder = r.source;
      } else {
        input.placeholder = "No Chrome found — paste path manually";
      }
      if (hint && Array.isArray(r.candidates) && r.candidates.length) {
        // Show other likely locations too. Useful on Windows where
        // Chrome vs Edge vs Brave vs Chromium all have different paths.
        const cList = r.candidates
          .map(c => `<code style="font-size:11px;">${escapeHtml(c)}</code>`)
          .join("<br>");
        hint.innerHTML =
          `Leave blank to use auto-detected path. Known locations on this OS:<br>${cList}`;
      }
    } catch (e) {
      console.warn("chrome-import discover:", e);
    }
  },

  async _runChromeImport() {
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    const btn     = document.getElementById("chrome-import-run-btn");
    const status  = document.getElementById("chrome-import-status");
    const srcEl   = document.getElementById("chrome-import-source");
    const daysEl  = document.getElementById("chrome-import-days");
    const maxEl   = document.getElementById("chrome-import-maxurls");
    const sensEl  = document.getElementById("chrome-import-sensitive");

    // "Source" may be either typed explicitly or we let backend auto-detect.
    // We send an empty string as null so the server uses discover_source().
    const source = (srcEl?.value || "").trim() || null;
    const days   = parseInt(daysEl?.value, 10) || 90;
    const maxUrls = parseInt(maxEl?.value, 10) || 5000;

    if (!await confirmDialog({
      title: "🧠 Import Chrome history?",
      message:
        `Copy real browsing history from Chrome into <strong>${escapeHtml(this.currentProfile)}</strong>.<br><br>` +
        `Your Chrome can stay open — we read a live snapshot.<br>` +
        `Will import URLs from last <strong>${days}</strong> days, up to ` +
        `<strong>${maxUrls}</strong> URLs, ${sensEl?.checked ? "skipping" : "<strong>keeping</strong>"} ` +
        `sensitive domains (banking/health/signed-in social).`,
      confirmText: "Import",
    })) return;

    if (btn) {
      btn.disabled = true;
      btn.textContent = "importing…";
    }
    if (status) {
      status.textContent = "reading source DB…";
      status.style.color = "";
    }

    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/chrome-import`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source,
            days,
            max_urls:       maxUrls,
            skip_sensitive: !!sensEl?.checked,
          }),
        }
      );
      if (r.ok) {
        const s = r.summary || {};
        const parts = [];
        if (s.history)     parts.push(`${s.history} URLs`);
        if (s.bookmarks)   parts.push(`${s.bookmarks} bookmarks`);
        if (s.preferences) parts.push("prefs");
        if (s.top_sites)   parts.push("top sites");
        const msg = parts.length ? parts.join(" · ") : "nothing found to import";
        if (status) {
          status.textContent = "✓ imported: " + msg;
          status.style.color = "var(--ok, #10b981)";
        }
        toast(`Chrome data imported: ${msg}`);
      } else {
        throw new Error(r.error || "import failed");
      }
    } catch (e) {
      const msg = e.message || "import failed";
      if (status) {
        status.textContent = "✗ " + msg;
        status.style.color = "var(--critical)";
      }
      toast(msg, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "📥 Import from Chrome";
      }
    }
  },

  // ─── PER-PROFILE META (tags, proxy, notes) ───────────────────
  //
  // The profile detail page edits dashboard-level metadata that lives
  // in the `profiles` table (not in the global config_kv). This is
  // different from the top "Main settings" card which still edits
  // global config. Every field here is optional — empty = inherit
  // global / pool value.
  //
  // Tags get their own tiny editor (chips + input) because users
  // manipulate them one at a time. Everything else is a plain input
  // bound to profiles/<name>/meta payload.

  _workingTags: [],

  async loadProfileMeta(name) {
    if (!name) return;
    this._metaProfileName = name;
    // RC-25 fix: disable Save buttons until the load completes.
    // Without this, a fast typist could open the form, type before
    // load resolves, hit Save — and write back the still-empty
    // payload, wiping the saved values they just opened. Disabling
    // tells the user "wait, reading state".
    const saveBtns = [
      document.getElementById("pp-save-btn"),
      document.getElementById("pp-proxy-save-btn-new"),
    ].filter(Boolean);
    saveBtns.forEach(b => {
      b.disabled = true;
      // Track the original label so we can restore it. data-attr is
      // safe across re-renders — survives DOM mutation by other
      // handlers.
      if (!b.dataset._origLabel) b.dataset._origLabel = b.textContent;
      b.textContent = "Loading…";
    });
    const restoreSaveBtns = () => {
      saveBtns.forEach(b => {
        b.disabled = false;
        if (b.dataset._origLabel) {
          b.textContent = b.dataset._origLabel;
        }
      });
    };
    try {
      const meta = await api(`/api/profiles/${encodeURIComponent(name)}/meta`);
      this._workingTags = Array.isArray(meta.tags) ? meta.tags.slice() : [];
      this._renderTagChips();
      const byId = (id) => document.getElementById(id);
      if (byId("pp-proxy-url"))         byId("pp-proxy-url").value         = meta.proxy_url         || "";
      if (byId("pp-rotation-url"))      byId("pp-rotation-url").value      = meta.rotation_api_url  || "";
      if (byId("pp-rotation-provider")) byId("pp-rotation-provider").value = meta.rotation_provider || "";
      if (byId("pp-rotation-api-key"))  byId("pp-rotation-api-key").value  = meta.rotation_api_key  || "";
      if (byId("pp-notes"))             byId("pp-notes").value             = meta.notes             || "";

      // D5-UI: render the needs_attention banner if main.py blocked
      // a run because of static-proxy + burn. The banner shows the
      // reason (e.g. "Captcha in 100% of searches (24h) — profile
      // burned") + when it happened + a button to clear the flag.
      // Hidden when needs_attention=0/null.
      this._renderAttentionBanner(name, meta);

      // Restore the "rotating proxy" checkbox state — was missing entirely,
      // so the toggle visually reset to OFF after every reload regardless
      // of saved value. Toggling the checkbox programmatically does NOT
      // fire the 'change' event, so we also manually expand/collapse the
      // rotation block so the UI matches the persisted state.
      const rotChk = byId("pp-proxy-rotating");
      if (rotChk) {
        rotChk.checked = !!meta.proxy_is_rotating;
        const block = byId("pp-rotation-block");
        if (block) block.style.display = rotChk.checked ? "" : "none";
      }
      const status = byId("pp-save-status");
      if (status) status.textContent = "";
      // Wire the asocks Auto-fill button. Idempotent -- safe to call
      // every time meta loads (we tag the element after first wire).
      this._wireAsocksAutofill();
      this._refreshAsocksDiscoverVisibility();
    } catch (e) {
      // 404 is OK — just means no custom metadata yet
      this._workingTags = [];
      this._renderTagChips();
    } finally {
      // RC-25: re-enable Save buttons regardless of success/failure
      // so the user can save once they've reviewed the form.
      restoreSaveBtns();
    }
  },

  // ── asocks rotation URL auto-discovery (per-profile) ─────────
  // Surfaces the same /api/proxy/asocks-port-list endpoint the
  // global Proxy edit modal already uses, but here we ALSO match
  // the profile's proxy URL host:port to a port and auto-pick that
  // port's refresh_link. Saves the user from having to copy a URL
  // they could already infer from the proxy URL they pasted above.
  _wireAsocksAutofill() {
    const btn   = document.getElementById("pp-asocks-discover-btn");
    const sel   = document.getElementById("pp-rotation-provider");
    const url   = document.getElementById("pp-proxy-url");
    if (!btn || btn.dataset._wired === "1") {
      // Still need to refresh visibility on every meta-load even if
      // already wired (provider may have changed across profiles).
      this._refreshAsocksDiscoverVisibility();
      return;
    }
    btn.dataset._wired = "1";
    btn.addEventListener("click", () => this._asocksAutofill());
    sel?.addEventListener("change", () => this._refreshAsocksDiscoverVisibility());
    url?.addEventListener("input",  () => this._refreshAsocksDiscoverVisibility());

    // Auto-trigger heuristic: when the user PICKS asocks from the
    // dropdown AND the proxy URL is filled AND the rotation URL is
    // empty -- silently kick off discovery so the field fills before
    // the user reaches for the manual button. Throttled with the
    // _autoTriedFor marker so we don't spam asocks's API on every
    // keystroke.
    sel?.addEventListener("change", () => {
      const provider = sel.value;
      const proxy    = (url?.value || "").trim();
      const rotUrl   = document.getElementById("pp-rotation-url")?.value?.trim();
      if (provider === "asocks" && proxy && !rotUrl &&
          this._autoTriedFor !== proxy) {
        this._autoTriedFor = proxy;
        this._asocksAutofill({silentIfNoKey: true});
      }
    });
  },

  _refreshAsocksDiscoverVisibility() {
    const btn = document.getElementById("pp-asocks-discover-btn");
    const sel = document.getElementById("pp-rotation-provider");
    if (!btn || !sel) return;
    btn.style.display = (sel.value === "asocks") ? "" : "none";
  },

  async _asocksAutofill(opts = {}) {
    const url   = (document.getElementById("pp-proxy-url")?.value || "").trim();
    const rotUrl = document.getElementById("pp-rotation-url");
    const keyEl  = document.getElementById("pp-rotation-api-key");
    const btn    = document.getElementById("pp-asocks-discover-btn");

    if (!url) {
      if (!opts.silentIfNoKey) {
        toast("Fill in the Proxy URL first — Auto-fill matches it to your asocks ports", true);
      }
      return;
    }
    // Parse host:port out of the proxy URL. Accept both with and
    // without scheme; with or without user:pass@.
    let host = "", port = "";
    try {
      let s = url.replace(/^[a-z0-9+]+:\/\//i, "");
      if (s.includes("@")) s = s.split("@").pop();
      // Could be host:port or [v6]:port -- handle simple v4 only here.
      const parts = s.split(":");
      if (parts.length >= 2) {
        host = parts[0];
        port = (parts[1] || "").split(/[/?#]/)[0];
      }
    } catch {}

    if (!host || !port) {
      if (!opts.silentIfNoKey) {
        toast("Could not parse host:port from Proxy URL", true);
      }
      return;
    }

    // Resolve the API key. Priority order:
    //   1. The per-profile API key field on this page (if filled)
    //   2. The global proxy.rotation_api_key from /api/config
    //   3. Prompt the user inline (only on explicit click)
    let apiKey = (keyEl?.value || "").trim();
    if (!apiKey) {
      try {
        const cfg = await api("/api/config");
        apiKey = (cfg && (cfg["proxy.rotation_api_key"] || cfg.proxy_rotation_api_key)) || "";
      } catch {}
    }
    if (!apiKey) {
      if (opts.silentIfNoKey) {
        // Auto-trigger path: don't pop a prompt on a UX surface the
        // user didn't explicitly engage. The button is visible -- they
        // can click it manually.
        return;
      }
      apiKey = (window.prompt(
        "Paste your asocks API key (one-time; we won't persist it unless you fill the Provider API key field).",
        ""
      ) || "").trim();
      if (!apiKey) return;
    }

    if (btn) {
      btn.disabled = true;
      btn.dataset._origText = btn.textContent;
      btn.textContent = "⏳ Searching…";
    }
    try {
      const resp = await api("/api/proxy/asocks-port-list", {
        method: "POST",
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!resp.ok) {
        toast(`asocks API error: ${resp.error || "unknown"}`, true);
        return;
      }
      const ports = resp.ports || [];
      if (!ports.length) {
        toast("No ports found on this asocks account", true);
        return;
      }
      // Match by both host AND port. The proxy URL's host:port
      // uniquely identifies one asocks port.
      const matches = ports.filter(p =>
        String(p.host || "") === host &&
        String(p.port || "") === String(port)
      );
      if (!matches.length) {
        toast(
          `No asocks port matches ${host}:${port}. Check that this proxy is in your asocks account.`,
          true
        );
        return;
      }
      const m = matches[0];
      if (m.refresh_link && rotUrl) {
        rotUrl.value = m.refresh_link;
        toast(`✓ Filled rotation URL from asocks port #${m.id || "?"} (${m.country || "?"})`);
      } else {
        toast(
          `Found port #${m.id || "?"} but it has no refresh_link. ` +
          `Check the rotation settings on the asocks dashboard.`,
          true
        );
      }
    } catch (e) {
      toast(`Auto-fill failed: ${e.message || e}`, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = btn.dataset._origText || "🔍 Auto-fill";
      }
    }
  },

  async saveProfileMeta() {
    if (!this._metaProfileName) {
      toast("No profile selected", true);
      return;
    }
    const byId = (id) => document.getElementById(id);
    const provider = byId("pp-rotation-provider")?.value || "";
    // proxy_is_rotating + rotation_api_key were silently dropped before —
    // the checkbox and the API-key input were rendered but their values
    // never made it into the POST payload. Result: ticking "rotating
    // proxy" had no effect, and the saved key was wiped to NULL on every
    // Save. Both are now first-class fields. Backend whitelist already
    // accepts them (see api_profile_meta_set in dashboard/server.py).
    const payload = {
      tags:              this._workingTags,
      proxy_url:         (byId("pp-proxy-url")?.value    || "").trim() || null,
      proxy_is_rotating: byId("pp-proxy-rotating")?.checked ? 1 : 0,
      rotation_api_url:  (byId("pp-rotation-url")?.value || "").trim() || null,
      rotation_api_key:  (byId("pp-rotation-api-key")?.value || "").trim() || null,
      // Empty string in the <select> means "inherit global", so send null.
      rotation_provider: provider || null,
      notes:             (byId("pp-notes")?.value || "").trim() || null,
    };
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this._metaProfileName)}/meta`,
        { method: "POST", body: JSON.stringify(payload) },
      );
      const status = byId("pp-save-status");
      if (status) {
        status.textContent = "✓ Saved";
        status.style.color = "#6ee7b7";
        setTimeout(() => { status.textContent = ""; }, 3000);
      }
      toast("✓ Profile overrides saved");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  _addTag() {
    const inp = document.getElementById("pp-tag-input");
    const raw = (inp?.value || "").trim();
    if (!raw) return;
    // Allow comma-separated batch entry: "a, b, c"
    raw.split(",").forEach(t => {
      const clean = t.trim();
      if (!clean) return;
      if (!this._workingTags.some(x => x.toLowerCase() === clean.toLowerCase())) {
        this._workingTags.push(clean);
      }
    });
    inp.value = "";
    this._renderTagChips();
  },

  _renderTagChips() {
    const container = document.getElementById("pp-tags-chips");
    if (!container) return;
    if (!this._workingTags.length) {
      container.innerHTML = `<span class="muted" style="font-size: 12px;">
        No tags yet — add some below.
      </span>`;
      return;
    }
    container.innerHTML = this._workingTags.map(t => `
      <span class="profile-tag-chip editor">
        ${escapeHtml(t)}
        <span class="profile-tag-chip-x" data-tag="${escapeHtml(t)}">×</span>
      </span>
    `).join("");
    container.querySelectorAll(".profile-tag-chip-x").forEach(x => {
      x.addEventListener("click", (e) => {
        const t = e.target.dataset.tag;
        this._workingTags = this._workingTags.filter(
          x => x.toLowerCase() !== t.toLowerCase()
        );
        this._renderTagChips();
      });
    });
  },

  // ─── COOKIES ────────────────────────────────────────────────
  //
  // Cookies live in profiles/<n>/ghostshell_session/cookies.json.
  // When a profile runs, ghost_shell_browser.py loads them into
  // Chrome via driver.add_cookie(); when it stops, the session_manager
  // writes them back. Here in the dashboard we read/write that file
  // directly — no Chrome needed.

  _cookieCache:   [],
  _cookieFilter:  "",

  async loadCookies(name) {
    if (!name) return;
    try {
      const data = await api(`/api/profiles/${encodeURIComponent(name)}/cookies`);
      this._cookieCache = data.cookies || [];
      const badge = document.getElementById("cookies-count-badge");
      if (badge) badge.textContent = String(data.count);
      this._renderCookies();
      this._updateCookieWarning(name);
    } catch (e) {
      console.warn("Failed to load cookies:", e);
      this._cookieCache = [];
      this._renderCookies();
    }
  },

  /** Show a yellow warning banner if the profile is currently running —
   *  changes to cookies.json won't apply until Chrome restarts. */
  async _updateCookieWarning(name) {
    const w = document.getElementById("cookies-warning-running");
    if (!w) return;
    try {
      const active = await api("/api/runs/active");
      const running = (active.runs || []).some(r => r.profile_name === name);
      w.style.display = running ? "" : "none";
    } catch {
      w.style.display = "none";
    }
  },

  _renderCookies() {
    const tbody = document.getElementById("cookies-tbody");
    const vcount = document.getElementById("cookies-visible-count");
    if (!tbody) return;

    const filter = this._cookieFilter || "";
    const filtered = !filter
      ? this._cookieCache
      : this._cookieCache.filter(c => {
          const hay = `${c.name || ""} ${c.domain || ""}`.toLowerCase();
          return hay.includes(filter);
        });

    if (vcount) vcount.textContent = String(filtered.length);

    if (!this._cookieCache.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state"
        style="padding: 24px; text-align: center;">
        No cookies stored. Import from a browser extension export,
        or run this profile — cookies collected during browsing are
        persisted here on exit.
      </td></tr>`;
      return;
    }
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted"
        style="padding: 14px; text-align: center;">
        No cookies match the filter.
      </td></tr>`;
      return;
    }

    tbody.innerHTML = filtered.map(c => {
      const expiry = c.expiry
        ? new Date(c.expiry * 1000).toISOString().slice(0, 10)
        : `<span class="muted">session</span>`;
      const flags = [];
      if (c.secure)   flags.push(`<span class="cookie-flag secure" title="Secure">🔒</span>`);
      if (c.httpOnly) flags.push(`<span class="cookie-flag httponly" title="HttpOnly">H</span>`);
      if (c.sameSite) flags.push(`<span class="cookie-flag samesite" title="SameSite=${escapeHtml(c.sameSite)}">${escapeHtml(c.sameSite[0] || "")}</span>`);

      // Value truncation — full value in title attribute
      const val = String(c.value || "");
      const shortVal = val.length > 40 ? val.slice(0, 37) + "…" : val;

      return `<tr>
        <td><strong>${escapeHtml(c.name || "")}</strong></td>
        <td><code class="cookie-domain">${escapeHtml(c.domain || "")}</code></td>
        <td><code class="cookie-value" title="${escapeHtml(val)}">${escapeHtml(shortVal)}</code></td>
        <td>${expiry}</td>
        <td>${flags.join(" ") || "<span class='muted'>—</span>"}</td>
        <td>
          <button class="cookie-row-delete"
                  onclick="ProfileDetail._deleteCookie('${escapeHtml(c.name || "")}')"
                  title="Delete all cookies with this name">×</button>
        </td>
      </tr>`;
    }).join("");
  },

  async _deleteCookie(name) {
    if (!this.currentProfile) return;
    const ok = await confirmDialog({
      title:   "Delete cookie",
      message: `Delete all cookies named "${name}" for this profile?`,
      confirmText: "Delete",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/cookies/${encodeURIComponent(name)}`,
        { method: "DELETE" }
      );
      toast("✓ Deleted");
      await this.loadCookies(this.currentProfile);
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async _clearCookies() {
    if (!this.currentProfile) return;
    const ok = await confirmDialog({
      title:   "Clear all cookies",
      message: "Remove every stored cookie for this profile?\n\nThe profile will browse as logged-out on its next start.",
      confirmText: "Clear all",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/cookies/clear`,
        { method: "POST" }
      );
      toast("✓ Cookies cleared");
      await this.loadCookies(this.currentProfile);
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  /** Download cookies as a JSON file. We let the browser handle
   *  the actual download via a hidden anchor click. */
  _exportCookies() {
    if (!this.currentProfile) return;
    const url = `/api/profiles/${encodeURIComponent(this.currentProfile)}/cookies/export?format=json`;
    const a = document.createElement("a");
    a.href = url;
    a.download = `cookies-${this.currentProfile}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("✓ Downloading…");
  },

  _openCookieImport() {
    const m = document.getElementById("cookie-import-modal");
    if (!m) return;
    document.getElementById("cookie-import-textarea").value = "";
    document.getElementById("cookie-import-file").value = "";
    // Default to merge mode
    const mergeRadio = document.querySelector(
      'input[name="cookie-import-mode"][value="merge"]'
    );
    if (mergeRadio) mergeRadio.checked = true;
    m.style.display = "flex";
  },

  _closeCookieImport() {
    const m = document.getElementById("cookie-import-modal");
    if (m) m.style.display = "none";
  },

  /** Read the selected file into the textarea so the user sees what
   *  they're about to import. Keeps the import flow uniform —
   *  everything ultimately goes through the textarea. */
  _handleCookieFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      document.getElementById("cookie-import-textarea").value = e.target.result || "";
    };
    reader.onerror = () => toast("Failed to read file", true);
    reader.readAsText(file);
  },

  async _submitCookieImport() {
    if (!this.currentProfile) return;
    const blob = document.getElementById("cookie-import-textarea").value || "";
    if (!blob.trim()) {
      toast("Nothing to import — paste cookies or pick a file first", true);
      return;
    }
    const mode = document.querySelector(
      'input[name="cookie-import-mode"]:checked'
    )?.value || "merge";

    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/cookies/import`,
        { method: "POST", body: JSON.stringify({ blob, mode }) }
      );
      toast(`✓ Imported ${r.imported_total}, ${r.added} new (total ${r.count})`);
      this._closeCookieImport();
      await this.loadCookies(this.currentProfile);
    } catch (e) {
      toast("Import error: " + e.message, true);
    }
  },

  // ── ACTIVE SCRIPT (per-profile script assignment) ───────────
  //
  // Loads the full scripts library to populate the dropdown, plus
  // the currently-assigned script for this profile (resolved via
  // /api/profiles/<name>/script which falls back to default).
  //
  // Save action calls /api/profiles/<name>/script with the selected
  // script_id. An empty value means "unassign" → server stores NULL
  // → runtime falls back to the default script.

  async loadActiveScript(name) {
    if (!name) return;
    const select = document.getElementById("profile-script-select");
    if (!select) return;
    try {
      // Load all scripts + current assignment in parallel
      const [listResp, activeResp] = await Promise.all([
        api("/api/scripts"),
        api(`/api/profiles/${encodeURIComponent(name)}/script`),
      ]);
      const scripts = listResp.scripts || [];
      const active  = activeResp.script;

      if (!scripts.length) {
        select.innerHTML = `<option value="">— no scripts —</option>`;
        select.disabled = true;
        return;
      }
      select.disabled = false;

      // "" means unassigned → default. Label shows which is default
      // so users see the implicit choice.
      const defaultScript = scripts.find(s => s.is_default);
      const defaultLabel = defaultScript
        ? `— use default (${defaultScript.name}) —`
        : "— use default —";

      const options = [
        `<option value="">${escapeHtml(defaultLabel)}</option>`,
        ...scripts.map(s => {
          const marker = s.is_default ? " ★" : "";
          return `<option value="${s.id}">${escapeHtml(s.name)}${marker}</option>`;
        }),
      ];
      select.innerHTML = options.join("");

      // Figure out what "active" really means. The API always returns
      // the resolved script (falling back to default), so to know
      // whether the profile has an explicit assignment we'd need a
      // separate flag. Simplest: mark the resolved one as selected.
      if (active?.id) {
        select.value = String(active.id);
      } else {
        select.value = "";
      }

      // Phase 5.1: load use_script_on_launch from profile meta and
      // sync the toggle + picker visibility. Default OFF when meta
      // hasn't been written yet (e.g. legacy profiles upgraded in
      // place from before the column existed).
      try {
        const meta = await api(`/api/profiles/${encodeURIComponent(name)}/meta`);
        // /api/profiles/<name>/meta returns a flat dict (see
        // api_profile_meta_get in dashboard/server.py — `return jsonify(meta)`),
        // not {meta: {...}}. The `meta.meta?.X` form was a leftover from
        // an older response shape and quietly evaluated to undefined →
        // the toggle always rendered OFF regardless of saved state.
        const useScript = !!meta.use_script_on_launch;
        const toggle = document.getElementById("profile-use-script-toggle");
        const wrap   = document.getElementById("profile-script-pick-wrap");
        if (toggle) {
          toggle.checked = useScript;
          toggle.onchange = () => {
            if (wrap) wrap.style.display = toggle.checked ? "" : "none";
          };
        }
        if (wrap) wrap.style.display = useScript ? "" : "none";
      } catch (e) {
        console.warn("use_script_on_launch load failed:", e);
      }

      // Hint updates on change
      const updateHint = () => {
        const hint = document.getElementById("profile-script-hint");
        const sel = select.value;
        if (!sel) {
          hint.textContent = "Will fall back to whichever script is marked default.";
        } else {
          const chosen = scripts.find(s => String(s.id) === sel);
          hint.textContent = chosen?.description
            || "Assigned script — open Scripts page to edit.";
        }
      };
      select.onchange = updateHint;
      updateHint();
    } catch (e) {
      console.error("loadActiveScript:", e);
      select.innerHTML = `<option value="">— error loading —</option>`;
    }
  },

  async saveActiveScript() {
    if (!this.currentProfile) {
      toast("Select a profile first", true);
      return;
    }
    const select = document.getElementById("profile-script-select");
    if (!select) return;
    const toggle = document.getElementById("profile-use-script-toggle");
    const useScript = toggle ? !!toggle.checked : false;
    const raw = select.value;
    const scriptId = raw === "" ? null : Number(raw);
    const btn = document.getElementById("profile-script-save-btn");
    btn.disabled = true;
    try {
      // Persist both: the script_id binding AND the opt-in flag.
      // Two endpoints because they target different SQL columns and
      // we don't want to fold meta-write into /script (which has
      // its own validation).
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/script`,
        {
          method: "POST",
          body: JSON.stringify({ script_id: useScript ? scriptId : null }),
        }
      );
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/meta`,
        {
          method: "POST",
          body: JSON.stringify({ use_script_on_launch: useScript ? 1 : 0 }),
        }
      );
      toast(useScript ? "✓ Script enabled on launch" : "✓ Script-on-launch disabled");
    } catch (e) {
      toast("Save failed: " + e.message, true);
    } finally {
      btn.disabled = false;
    }
  },

  // ── ACTIVE PROXY (per-profile proxy assignment) ─────────────
  //
  // Same pattern as scripts: load library + current assignment in
  // parallel, render dropdown + live status of the resolved proxy.

  async loadActiveProxy(name) {
    if (!name) return;
    const select = document.getElementById("profile-proxy-select");
    if (!select) return;
    try {
      const [listResp, activeResp] = await Promise.all([
        api("/api/proxies"),
        api(`/api/profiles/${encodeURIComponent(name)}/proxy`),
      ]);
      const proxies = listResp.proxies || [];
      const active  = activeResp.proxy;

      if (!proxies.length) {
        select.innerHTML = `<option value="">— no proxies — create one first</option>`;
        select.disabled = true;
        this._renderProxyStatus(null);
        return;
      }
      select.disabled = false;

      const defaultProxy = proxies.find(p => p.is_default);
      const defaultLabel = defaultProxy
        ? `— use default (${defaultProxy.name}) —`
        : "— use default —";

      const options = [
        `<option value="">${escapeHtml(defaultLabel)}</option>`,
        ...proxies.map(p => {
          const marker = p.is_default ? " ★" : "";
          // Include host:port in label for disambiguation when names
          // collide or are missing
          const label = `${p.name}${marker}`;
          return `<option value="${p.id}">${escapeHtml(label)}</option>`;
        }),
      ];
      select.innerHTML = options.join("");
      select.value = active?.id ? String(active.id) : "";

      // Live status of the currently-resolved proxy
      this._renderProxyStatus(active);

      // Description hint on change
      const updateHint = () => {
        const hint = document.getElementById("profile-proxy-hint");
        const sel = select.value;
        if (!sel) {
          hint.textContent = "Will fall back to whichever proxy is marked default.";
        } else {
          const chosen = proxies.find(p => String(p.id) === sel);
          if (chosen) {
            const parts = [
              chosen.host && chosen.port ? `${chosen.host}:${chosen.port}` : "",
              chosen.last_country || "",
              chosen.is_rotating ? "rotating" : "",
            ].filter(Boolean);
            hint.textContent = parts.join(" · ") || "—";
          }
        }
      };
      select.onchange = updateHint;
      updateHint();
    } catch (e) {
      console.error("loadActiveProxy:", e);
      select.innerHTML = `<option value="">— error loading —</option>`;
    }
  },

  _renderProxyStatus(proxy) {
    const row = document.getElementById("profile-proxy-status");
    if (!row) return;
    if (!proxy) {
      row.style.display = "none";
      return;
    }
    row.style.display = "";
    const badge = document.getElementById("profile-proxy-status-badge");
    const meta  = document.getElementById("profile-proxy-status-meta");
    const status = proxy.last_status || "untested";
    badge.className = `status-badge status-${status}`;
    badge.textContent = status === "ok" ? "ACTIVE"
                       : status === "error" ? "ERROR" : "UNTESTED";
    const parts = [];
    if (proxy.host && proxy.port) parts.push(`${proxy.host}:${proxy.port}`);
    if (proxy.last_country) parts.push(proxy.last_country);
    if (proxy.is_rotating) parts.push("↻ rotating");
    meta.textContent = parts.join(" · ");
  },

  async saveActiveProxy() {
    if (!this.currentProfile) {
      toast("Select a profile first", true);
      return;
    }
    const select = document.getElementById("profile-proxy-select");
    if (!select) return;
    const raw = select.value;
    const proxyId = raw === "" ? null : Number(raw);
    const btn = document.getElementById("profile-proxy-save-btn");
    btn.disabled = true;
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/proxy`,
        {
          method: "POST",
          body: JSON.stringify({ proxy_id: proxyId }),
        }
      );
      toast("✓ Proxy assigned");
      // Reload to refresh status display
      await this.loadActiveProxy(this.currentProfile);
    } catch (e) {
      toast("Save failed: " + e.message, true);
    } finally {
      btn.disabled = false;
    }
  },

  async testActiveProxy() {
    const select = document.getElementById("profile-proxy-select");
    if (!select) return;
    // Use the currently-selected one, OR the default if "" is selected
    let proxyId = select.value ? Number(select.value) : null;
    if (!proxyId) {
      // Fetch default
      try {
        const listResp = await api("/api/proxies");
        const def = (listResp.proxies || []).find(p => p.is_default);
        if (!def) {
          toast("No default proxy configured", true);
          return;
        }
        proxyId = def.id;
      } catch {
        toast("Could not resolve default", true);
        return;
      }
    }
    const btn = document.getElementById("profile-proxy-test-btn");
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "⏳ Testing…";
    try {
      const resp = await api(`/api/proxies/${proxyId}/test`, { method: "POST" });
      const diag = resp.diag || {};
      if (diag.ok) {
        toast(`✓ ${diag.country || "OK"} · ${diag.latency_ms}ms`);
      } else {
        toast(`✗ ${diag.error || "failed"}`, true);
      }
      await this.loadActiveProxy(this.currentProfile);
    } catch (e) {
      toast(`Test failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  },

  // ─── Cookie pool button + modal (Task #26) ────────────────────
  // Lets the user inject cookies from another profile's snapshot
  // into THIS profile, browserless. Handy when you've warmed a
  // throwaway "donor" profile and want to seed real-looking cookies
  // into a freshly-created production profile so its first request
  // doesn't look like a brand-new browser.
  //
  // Flow:
  //   1. Open modal → fetch /api/cookies/pool/match  (auto-pick)
  //                +  /api/cookies/pool             (full list)
  //   2. User picks a snapshot (or accepts the auto-recommendation)
  //   3. POST /api/cookies/pool/inject — backend writes cookies.json
  //      into target profile's session_dir; next launch restores
  //      them via the existing CDP cookie-load path.
  _wireCookiePoolBtn() {
    const btn = document.getElementById("profile-cookie-pool-btn");
    if (!btn || btn.dataset._wired === "1") return;
    btn.dataset._wired = "1";
    btn.addEventListener("click", () => this._openCookiePoolModal());

    document.querySelectorAll('[data-close="cookie-pool-modal"]').forEach(el => {
      el.addEventListener("click", () => {
        const m = document.getElementById("cookie-pool-modal");
        if (m) m.style.display = "none";
      });
    });

    const injectBtn = document.getElementById("cookie-pool-inject-btn");
    if (injectBtn && injectBtn.dataset._wired !== "1") {
      injectBtn.dataset._wired = "1";
      injectBtn.addEventListener("click", () => this._cookiePoolInject());
    }
  },

  async _openCookiePoolModal() {
    const modal = document.getElementById("cookie-pool-modal");
    const body  = document.getElementById("cookie-pool-modal-body");
    const inject = document.getElementById("cookie-pool-inject-btn");
    if (!modal || !body) return;
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }

    body.innerHTML = `<div class="muted">Loading pool…</div>`;
    if (inject) {
      inject.disabled = true;
      inject.textContent = "Inject selected";
    }
    this._selectedSnapshotId = null;
    modal.style.display = "";

    // Pull expected_country off this profile so the auto-pick is
    // locale-aware (UA cookies onto a UA profile, etc). Fall back
    // to global default if the profile didn't set one.
    let country = null;
    try {
      const meta = await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/meta`);
      country = (meta?.expected_country) || null;
    } catch {}
    if (!country) {
      try {
        const cfg = await api("/api/config");
        country = (cfg && cfg["browser.expected_country"]) || null;
      } catch {}
    }
    this._cookiePoolCountry = country;

    // Auto-pick + full list, in parallel
    let match = null, list = [];
    try {
      const m = await api(
        "/api/cookies/pool/match" +
        (country ? `?country=${encodeURIComponent(country)}` : "?") +
        `${country ? "&" : ""}exclude_profile=${encodeURIComponent(this.currentProfile)}`
      );
      match = m?.match || null;
    } catch (e) {
      console.warn("pool match failed:", e);
    }
    try {
      const l = await api(
        "/api/cookies/pool" +
        (country ? `?country=${encodeURIComponent(country)}` : "?") +
        `${country ? "&" : ""}exclude_profile=${encodeURIComponent(this.currentProfile)}` +
        "&limit=20"
      );
      list = l?.snapshots || [];
    } catch (e) {
      console.warn("pool list failed:", e);
    }

    body.innerHTML = this._renderCookiePoolModalBody(country, match, list);

    // Wire row selection
    body.querySelectorAll("[data-snap-id]").forEach(row => {
      row.addEventListener("click", () => {
        body.querySelectorAll("[data-snap-id]").forEach(r => r.classList.remove("selected"));
        row.classList.add("selected");
        this._selectedSnapshotId = parseInt(row.dataset.snapId, 10);
        if (inject) inject.disabled = false;
      });
    });

    // Pre-select the auto-pick so user can hit Inject without thinking
    if (match && match.id) {
      const r = body.querySelector(`[data-snap-id="${match.id}"]`);
      if (r) {
        r.classList.add("selected");
        this._selectedSnapshotId = match.id;
        if (inject) inject.disabled = false;
      }
    }
  },

  _renderCookiePoolModalBody(country, match, list) {
    const escapeHtml = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const matchBlock = match
      ? `<div class="cookie-pool-rec">
           <div class="cookie-pool-rec-title">⭐ Recommended pick</div>
           <div class="cookie-pool-rec-meta">
             <strong>${escapeHtml(match.profile_name)}</strong>
             — ${match.cookie_count || 0} cookies
             across ${match.domain_count || 0} domains,
             ${match.country ? escapeHtml(match.country) : "no country"}/${match.category ? escapeHtml(match.category) : "any"},
             ${match.created_at ? timeAgo(match.created_at) : "unknown age"}
           </div>
         </div>`
      : `<div class="muted" style="padding: 6px 0; font-size: 12px;">
           No auto-pick available${country ? ` for country <strong>${escapeHtml(country)}</strong>` : ""}.
           Pick from the list manually below.
         </div>`;

    if (!list.length) {
      return matchBlock + `
        <div class="dense-empty" style="padding: 30px 16px; text-align: center;">
          <div style="font-size: 28px; opacity: 0.4; margin-bottom: 8px;">🍪</div>
          <div style="font-size: 13px; opacity: 0.7;">
            No snapshots in the pool yet. Run a profile that triggers a
            cookie snapshot (warmup, search session, etc) first — then
            come back here to inject those cookies into other profiles.
          </div>
        </div>`;
    }

    const rows = list.map(s => `
      <div class="cookie-pool-row" data-snap-id="${s.id}">
        <div class="cookie-pool-row-name">
          ${escapeHtml(s.profile_name || "(unknown)")}
          ${s.country ? `<span class="cookie-pool-row-country">${escapeHtml(s.country)}</span>` : ""}
          ${s.category ? `<span class="cookie-pool-row-cat">${escapeHtml(s.category)}</span>` : ""}
        </div>
        <div class="cookie-pool-row-meta">
          <span><strong>${s.cookie_count || 0}</strong> cookies</span>
          <span><strong>${s.domain_count || 0}</strong> domains</span>
          <span class="muted">${s.created_at ? timeAgo(s.created_at) : "—"}</span>
          ${s.trigger ? `<span class="muted">via ${escapeHtml(s.trigger)}</span>` : ""}
        </div>
      </div>
    `).join("");

    return matchBlock + `
      <div class="form-hint" style="margin: 10px 0 6px;">
        Pick a snapshot to copy its cookies into <strong>${escapeHtml(this.currentProfile)}</strong>.
        The target profile's existing cookies are merged with the donor's
        — duplicates by (name, domain) are overwritten with the donor copy.
      </div>
      <div class="cookie-pool-list">${rows}</div>`;
  },

  async _cookiePoolInject() {
    if (!this.currentProfile) return;
    const btn = document.getElementById("cookie-pool-inject-btn");
    if (!btn) return;
    if (!this._selectedSnapshotId) {
      toast("Pick a snapshot first", true);
      return;
    }
    btn.disabled = true;
    btn.textContent = "Injecting…";
    try {
      const r = await api("/api/cookies/pool/inject", {
        method: "POST",
        body: JSON.stringify({
          target_profile: this.currentProfile,
          snapshot_id:    this._selectedSnapshotId,
        }),
      });
      if (r && r.ok !== false) {
        toast(
          `✓ Injected ${r.cookies_written || 0} new cookies` +
          (r.source_profile ? ` from ${r.source_profile}` : "") +
          (r.total_cookies_after ? ` (${r.total_cookies_after} total now)` : "")
        );
        const m = document.getElementById("cookie-pool-modal");
        if (m) m.style.display = "none";
        // Refresh the session summary so the new cookie count shows up
        try { await this.loadSessionSummary(this.currentProfile); } catch {}
      } else {
        toast(`Inject failed: ${(r && r.error) || "unknown"}`, true);
      }
    } catch (e) {
      toast(`Inject failed: ${e.message || e}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Inject selected";
    }
  },

  // ─── Extensions card (Phase 3) ─────────────────────────────────
  // Per-profile assignment from the shared Extensions pool. Renders
  // chips for what's currently assigned and opens a picker modal
  // listing every enabled pool entry. Toggling a card immediately
  // POSTs/DELETEs the assignment — at next launch runtime.py builds
  // --load-extension from this set.
  async loadProfileExtensions(name) {
    const chips = document.getElementById("profile-ext-chips");
    if (!chips) return;
    try {
      const [pool, assigned] = await Promise.all([
        api("/api/extensions").catch(() => ({ extensions: [] })),
        api(`/api/profiles/${encodeURIComponent(name)}/extensions`).catch(() => ({ extensions: [] })),
      ]);
      this._extPoolCache     = pool?.extensions || pool || [];
      this._extAssignedCache = (assigned?.extensions || assigned || []).map(r => ({
        extension_id: r.extension_id || r.id,
        enabled:      r.enabled !== 0 && r.enabled !== false,
      }));
      this._renderProfileExtChips();
    } catch (e) {
      chips.innerHTML = `<span class="muted" style="font-size: 12px; color:#fca5a5;">
        Failed to load extensions: ${escapeHtml(e.message)}
      </span>`;
    }
  },

  _renderProfileExtChips() {
    const chips = document.getElementById("profile-ext-chips");
    if (!chips) return;
    const assigned = this._extAssignedCache || [];
    const pool     = this._extPoolCache || [];
    if (!assigned.length) {
      chips.innerHTML = `<div class="profile-ext-empty">
        No extensions assigned. Click <strong>+ Add from pool</strong>
        to install one at next launch.
      </div>`;
      return;
    }
    chips.innerHTML = assigned.map(a => {
      const x = pool.find(p => p.id === a.extension_id);
      const name = x?.name || "(missing from pool)";
      const ver  = x?.version ? `v${x.version}` : "";
      const icon = x?.icon_b64
        ? `<img src="${(x?.icon_b64?.startsWith('data:') ? x.icon_b64 : 'data:image/png;base64,' + x.icon_b64)}" alt="">`
        : `🧩`;
      const disabled = !a.enabled || (x && (x.is_enabled === 0 || x.is_enabled === false));
      const tip = disabled
        ? "Disabled — won't load at next launch"
        : `Will be loaded with --load-extension at next launch${ver ? " (" + ver + ")" : ""}`;
      return `
        <span class="profile-ext-chip ${disabled ? "is-disabled" : ""}" title="${escapeHtml(tip)}">
          <span class="profile-ext-chip-icon">${icon}</span>
          <span class="profile-ext-chip-name">${escapeHtml(name)}</span>
          <button class="profile-ext-chip-x" data-eid="${escapeHtml(a.extension_id)}"
                  title="Remove from this profile">×</button>
        </span>`;
    }).join("");

    chips.querySelectorAll(".profile-ext-chip-x").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        this._removeProfileExt(btn.dataset.eid);
      });
    });
  },

  async _removeProfileExt(eid) {
    if (!this.currentProfile || !eid) return;
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/extensions/${encodeURIComponent(eid)}`,
        { method: "DELETE" }
      );
      this._extAssignedCache = (this._extAssignedCache || []).filter(a => a.extension_id !== eid);
      this._renderProfileExtChips();
      toast("Removed from profile");
    } catch (e) {
      toast(`Remove failed: ${e.message}`, true);
    }
  },

  _wireProfileExtBtn() {
    const btn = document.getElementById("profile-ext-add-btn");
    if (!btn || btn.dataset._wired === "1") return;
    btn.dataset._wired = "1";
    btn.addEventListener("click", () => this._openProfileExtModal());
    document.querySelectorAll('[data-close="profile-ext-modal"]').forEach(el => {
      el.addEventListener("click", () => {
        const m = document.getElementById("profile-ext-modal");
        if (m) m.style.display = "none";
      });
    });
  },

  async _openProfileExtModal() {
    const modal = document.getElementById("profile-ext-modal");
    const body  = document.getElementById("profile-ext-modal-body");
    if (!modal || !body) return;
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    modal.style.display = "";

    body.innerHTML = `<div class="muted" style="font-size: 12px;">Loading pool…</div>`;
    try {
      const [pool, assigned] = await Promise.all([
        api("/api/extensions"),
        api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/extensions`),
      ]);
      this._extPoolCache     = pool?.extensions || pool || [];
      this._extAssignedCache = (assigned?.extensions || assigned || []).map(r => ({
        extension_id: r.extension_id || r.id,
        enabled:      r.enabled !== 0 && r.enabled !== false,
      }));
    } catch (e) {
      body.innerHTML = `<div class="muted" style="color:#fca5a5;">Load failed: ${escapeHtml(e.message)}</div>`;
      return;
    }

    body.innerHTML = this._renderProfileExtModalBody();
    this._wireProfileExtModalCards();
  },

  _renderProfileExtModalBody() {
    const pool = (this._extPoolCache || []).filter(
      x => x.is_enabled !== 0 && x.is_enabled !== false
    );
    const assignedSet = new Set((this._extAssignedCache || []).map(a => a.extension_id));
    if (!pool.length) {
      return `
        <div class="dense-empty" style="padding: 30px 16px; text-align: center;">
          <div style="font-size: 28px; opacity: 0.4; margin-bottom: 8px;">📭</div>
          <div style="font-size: 13px; opacity: 0.75;">
            The pool is empty. Head to the
            <a href="#" data-nav="extensions" class="inline-link"
               onclick="document.getElementById('profile-ext-modal').style.display='none';"
            >Extensions page</a> to install your first extension.
          </div>
        </div>`;
    }

    const cards = pool.map(x => {
      const assigned = assignedSet.has(x.id);
      const icon = x.icon_b64
        ? `<img src="${(x.icon_b64.startsWith('data:') ? x.icon_b64 : 'data:image/png;base64,' + x.icon_b64)}" alt="">`
        : `<span style="font-size: 22px;">🧩</span>`;
      return `
        <div class="profile-ext-picker-card ${assigned ? "is-assigned" : ""}"
             data-eid="${escapeHtml(x.id)}">
          <div class="profile-ext-picker-icon">${icon}</div>
          <div style="min-width: 0;">
            <div class="profile-ext-picker-name" title="${escapeHtml(x.name)}">${escapeHtml(x.name || "(unnamed)")}</div>
            ${x.version ? `<div class="profile-ext-picker-version">v${escapeHtml(x.version)}</div>` : ""}
          </div>
          <div class="profile-ext-picker-check">${assigned ? "✓" : ""}</div>
        </div>`;
    }).join("");
    return `
      <div class="form-hint" style="margin-bottom: 8px;">
        Click an extension to toggle. Changes apply immediately —
        the next time you launch this profile, the assigned set is
        loaded into Chrome with <code>--load-extension</code>.
      </div>
      <div class="profile-ext-picker-grid">${cards}</div>
    `;
  },

  _wireProfileExtModalCards() {
    const body = document.getElementById("profile-ext-modal-body");
    if (!body) return;
    body.querySelectorAll(".profile-ext-picker-card").forEach(card => {
      card.addEventListener("click", async () => {
        const eid = card.dataset.eid;
        if (!eid) return;
        const wasAssigned = card.classList.contains("is-assigned");
        card.style.opacity = "0.55";
        try {
          if (wasAssigned) {
            await api(
              `/api/profiles/${encodeURIComponent(this.currentProfile)}/extensions/${encodeURIComponent(eid)}`,
              { method: "DELETE" }
            );
            this._extAssignedCache = (this._extAssignedCache || []).filter(a => a.extension_id !== eid);
            card.classList.remove("is-assigned");
            card.querySelector(".profile-ext-picker-check").textContent = "";
          } else {
            await api(
              `/api/profiles/${encodeURIComponent(this.currentProfile)}/extensions`,
              { method: "POST", body: JSON.stringify({ extension_id: eid, enabled: true }) }
            );
            (this._extAssignedCache = this._extAssignedCache || []).push({ extension_id: eid, enabled: true });
            card.classList.add("is-assigned");
            card.querySelector(".profile-ext-picker-check").textContent = "✓";
          }
          this._renderProfileExtChips();
        } catch (e) {
          toast(`Toggle failed: ${e.message}`, true);
        } finally {
          card.style.opacity = "";
        }
      });
    });
  },
};
