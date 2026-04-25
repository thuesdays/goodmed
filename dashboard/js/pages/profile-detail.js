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
      ]);
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
      $("#selfcheck-badge").textContent = `${sc.passed}/${sc.total}`;
      $("#selfcheck-time").textContent = `Last check: ${sc.timestamp}`;

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
    } catch (e) {
      $("#selfcheck-badge").textContent = "—";
      $("#selfcheck-grid").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
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
    try {
      const meta = await api(`/api/profiles/${encodeURIComponent(name)}/meta`);
      this._workingTags = Array.isArray(meta.tags) ? meta.tags.slice() : [];
      this._renderTagChips();
      const byId = (id) => document.getElementById(id);
      if (byId("pp-proxy-url"))         byId("pp-proxy-url").value         = meta.proxy_url         || "";
      if (byId("pp-rotation-url"))      byId("pp-rotation-url").value      = meta.rotation_api_url  || "";
      if (byId("pp-rotation-provider")) byId("pp-rotation-provider").value = meta.rotation_provider || "";
      if (byId("pp-notes"))             byId("pp-notes").value             = meta.notes             || "";
      const status = byId("pp-save-status");
      if (status) status.textContent = "";
    } catch (e) {
      // 404 is OK — just means no custom metadata yet
      this._workingTags = [];
      this._renderTagChips();
    }
  },

  async saveProfileMeta() {
    if (!this._metaProfileName) {
      toast("No profile selected", true);
      return;
    }
    const byId = (id) => document.getElementById(id);
    const provider = byId("pp-rotation-provider")?.value || "";
    const payload = {
      tags:              this._workingTags,
      proxy_url:         (byId("pp-proxy-url")?.value    || "").trim() || null,
      rotation_api_url:  (byId("pp-rotation-url")?.value || "").trim() || null,
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
      // If the user actually had no assignment, they'll see the
      // default entry pre-selected — which matches reality.
      if (active?.id) {
        select.value = String(active.id);
      } else {
        select.value = "";
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
    const raw = select.value;
    const scriptId = raw === "" ? null : Number(raw);
    const btn = document.getElementById("profile-script-save-btn");
    btn.disabled = true;
    try {
      await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}/script`,
        {
          method: "POST",
          body: JSON.stringify({ script_id: scriptId }),
        }
      );
      toast("✓ Script assigned");
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
};
