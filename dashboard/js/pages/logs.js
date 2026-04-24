// ═══════════════════════════════════════════════════════════════
// pages/logs.js — live SSE buffer + historical viewer with filters
//
// With multi-run, the SSE stream is a merge of many concurrent runs.
// Each log entry carries run_id + profile_name so the user can narrow
// the view to just one profile or one run without losing context.
//
// Filters are AND-combined: profile × level × free-text substring.
// ═══════════════════════════════════════════════════════════════

const Logs = {
  _unsubscribe:     null,
  _filterProfile:   "",
  _filterLevel:     "",
  _filterText:      "",
  _knownProfiles:   new Set(),

  async init() {
    if (this._unsubscribe) { this._unsubscribe(); this._unsubscribe = null; }

    const mode = window.LOGS_MODE || { type: "live" };
    this.applyMode(mode);

    // The global LOG_BUFFER may already contain data if runner.js
    // primed it before navigation — if so render immediately. If it's
    // empty (dashboard just loaded on /logs directly, or user cleared),
    // explicitly pull the ring buffer so the page is never blank
    // after reload.
    if (mode.type === "live" && LOG_BUFFER.length === 0) {
      try {
        await primeLogBuffer();
      } catch {}
    }

    // Seed the filter dropdown from whatever's in the buffer right now.
    this._rescanKnownProfiles();
    this._renderProfileDropdown();
    this.render();

    if (mode.type === "live") {
      this._unsubscribe = onLogEntry((entry) => {
        const pn = entry?.profile_name;
        if (pn && !this._knownProfiles.has(pn)) {
          this._knownProfiles.add(pn);
          this._renderProfileDropdown();
        }
        this.render();
      });
    }

    $("#clear-logs-btn").addEventListener("click", () => {
      LOG_BUFFER.length = 0;
      this._knownProfiles.clear();
      this._renderProfileDropdown();
      this.render();
      toast("✓ Cleared");
    });

    // Copy visible logs to clipboard. Honors current filters — if you
    // filtered to one profile, you get only that profile's lines.
    $("#copy-logs-btn")?.addEventListener("click", () => this.copyLogs());
    // Download visible logs as .txt. Filename includes active filter
    // + timestamp so multiple saves don't overwrite each other.
    $("#download-logs-btn")?.addEventListener("click", () => this.downloadLogs());

    $("#back-to-runs-btn").addEventListener("click", () => navigate("runs"));
    $("#switch-to-live-btn").addEventListener("click", () => {
      window.LOGS_MODE = { type: "live" };
      navigate("logs");
    });

    // Filter wiring
    $("#logs-filter-profile")?.addEventListener("change", (e) => {
      this._filterProfile = e.target.value;
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-level")?.addEventListener("change", (e) => {
      this._filterLevel = e.target.value;
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-text")?.addEventListener("input", (e) => {
      this._filterText = (e.target.value || "").toLowerCase();
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-reset")?.addEventListener("click", () => this._resetFilters());
  },

  applyMode(mode) {
    if (mode.type === "history") {
      $("#logs-title").textContent = `Logs for run #${mode.runId}`;
      $("#logs-subtitle").textContent = "Historical — stored in the database";
      $("#back-to-runs-btn").style.display = "inline-flex";
      $("#switch-to-live-btn").style.display = "inline-flex";
    } else {
      $("#logs-title").textContent = "Live logs";
      $("#logs-subtitle").textContent = "Merged output from all active runs (SSE)";
      $("#back-to-runs-btn").style.display = "none";
      $("#switch-to-live-btn").style.display = "none";
    }
  },

  _rescanKnownProfiles() {
    this._knownProfiles.clear();
    for (const l of LOG_BUFFER) {
      if (l.profile_name) this._knownProfiles.add(l.profile_name);
    }
  },

  _renderProfileDropdown() {
    const sel = $("#logs-filter-profile");
    if (!sel) return;
    const current = sel.value;
    const names = Array.from(this._knownProfiles).sort();
    sel.innerHTML = `<option value="">All profiles</option>` +
      names.map(n => {
        const isSel = n === current ? "selected" : "";
        return `<option value="${escapeHtml(n)}" ${isSel}>${escapeHtml(n)}</option>`;
      }).join("");
  },

  _updateFilterSummary() {
    const bar = $("#logs-filter-summary");
    const chipsEl = $("#logs-filter-chips");
    if (!bar || !chipsEl) return;

    const chips = [];
    if (this._filterProfile) chips.push(`profile: ${escapeHtml(this._filterProfile)}`);
    if (this._filterLevel)   chips.push(`level: ${escapeHtml(this._filterLevel)}`);
    if (this._filterText)    chips.push(`text: "${escapeHtml(this._filterText)}"`);

    if (!chips.length) {
      bar.style.display = "none";
    } else {
      bar.style.display = "";
      chipsEl.innerHTML = chips
        .map(c => `<span class="profile-tag-chip active">${c}</span>`)
        .join(" ");
    }
  },

  _resetFilters() {
    this._filterProfile = "";
    this._filterLevel   = "";
    this._filterText    = "";
    $("#logs-filter-profile").value = "";
    $("#logs-filter-level").value   = "";
    $("#logs-filter-text").value    = "";
    this._updateFilterSummary();
    this.render();
  },

  _passesFilter(l) {
    if (this._filterProfile && l.profile_name !== this._filterProfile) return false;
    if (this._filterLevel   && l.level         !== this._filterLevel)   return false;
    if (this._filterText) {
      const hay = (l.message || "").toLowerCase();
      if (!hay.includes(this._filterText)) return false;
    }
    return true;
  },

  /** Serialize the currently-visible (filtered) log lines to plain
   *  text. Format matches what appears on screen but without HTML:
   *    [timestamp] [LEVEL] [profile_name] message
   *  Profile tag is included always (not just when multi-profile
   *  chip is shown) so downloaded logs are self-documenting. */
  _visibleAsText() {
    const visible = LOG_BUFFER.filter(l => this._passesFilter(l));
    return visible.map(l => {
      const ts    = l.ts || "";
      const lvl   = (l.level || "info").toUpperCase();
      const pn    = l.profile_name ? `[${l.profile_name}] ` : "";
      const msg   = l.message || "";
      return `${ts} [${lvl}] ${pn}${msg}`;
    }).join("\n");
  },

  /** Builds a descriptive filename for the .txt download. Embeds the
   *  active filters + ISO timestamp so saving 5 times in a row produces
   *  5 differently-named files (no accidental overwrites). */
  _downloadFilename() {
    const parts = ["ghost-shell-logs"];
    if (this._filterProfile) parts.push(this._filterProfile);
    if (this._filterLevel)   parts.push(this._filterLevel);
    // ISO timestamp, colons → dashes so Windows filesystems accept it
    const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    parts.push(ts);
    return parts.join("_") + ".txt";
  },

  async copyLogs() {
    const text = this._visibleAsText();
    if (!text) {
      toast("Nothing to copy (buffer empty or filtered out)", true);
      return;
    }
    // Prefer the async Clipboard API — works on localhost + HTTPS.
    // Fallback via hidden textarea + execCommand covers http origins
    // and older browsers; it's ugly but bulletproof.
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      const lines = text.split("\n").length;
      toast(`✓ Copied ${lines.toLocaleString()} line${lines === 1 ? "" : "s"} to clipboard`);
    } catch (e) {
      toast("Copy failed: " + (e.message || e), true);
    }
  },

  downloadLogs() {
    const text = this._visibleAsText();
    if (!text) {
      toast("Nothing to download (buffer empty or filtered out)", true);
      return;
    }
    try {
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const url  = URL.createObjectURL(blob);
      // Temp <a> + click is the standard "programmatic download" trick.
      // Appending to document before click is required in some browsers
      // (Firefox historically wouldn't fire click() on detached nodes).
      const a = document.createElement("a");
      a.href = url;
      a.download = this._downloadFilename();
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Revoke the blob URL after a tick so the browser has time to
      // start the download. Immediate revoke races on Safari.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      const lines = text.split("\n").length;
      toast(`✓ Saved ${lines.toLocaleString()} line${lines === 1 ? "" : "s"} to ${a.download}`);
    } catch (e) {
      toast("Download failed: " + (e.message || e), true);
    }
  },

  render() {
    const box = $("#logs-box");
    if (!box) return;

    const visible = LOG_BUFFER.filter(l => this._passesFilter(l));

    if (!LOG_BUFFER.length) {
      box.innerHTML = '<div class="muted">No log entries</div>';
      return;
    }
    if (!visible.length) {
      box.innerHTML = `<div class="muted">
        No entries match the current filter (${LOG_BUFFER.length} total in buffer)
      </div>`;
      return;
    }

    // Only show the per-line profile chip when "All profiles" is active
    // AND we've seen >1 profile — otherwise it's noise.
    const showProfileChip = !this._filterProfile && this._knownProfiles.size > 1;

    box.innerHTML = visible.map(l => {
      const pn = l.profile_name;
      const chip = (showProfileChip && pn)
        ? `<span class="log-profile-chip">${escapeHtml(pn)}</span>`
        : "";
      return `<div class="log-line ${l.level || 'info'}">` +
             `<span class="ts">${escapeHtml(l.ts || '')}</span>` +
             chip +
             `<span class="msg">${escapeHtml(l.message || '')}</span>` +
             `</div>`;
    }).join("");
    box.scrollTop = box.scrollHeight;
  },

  teardown() {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
  },
};
