// ═══════════════════════════════════════════════════════════════
// pages/logs.js — live SSE buffer + historical run log viewer
// ═══════════════════════════════════════════════════════════════

const Logs = {
  _unsubscribe: null,

  // Mode state — set by Runs.viewLogs() before navigating here.
  // Stored globally so navigation doesn't lose it.
  async init() {
    if (this._unsubscribe) { this._unsubscribe(); this._unsubscribe = null; }

    const mode = window.LOGS_MODE || { type: "live" };
    this.applyMode(mode);
    this.render();

    if (mode.type === "live") {
      // Subscribe to live stream
      this._unsubscribe = onLogEntry(() => this.render());
    }

    $("#clear-logs-btn").addEventListener("click", () => {
      LOG_BUFFER.length = 0;
      this.render();
      toast("✓ Cleared");
    });
    $("#back-to-runs-btn").addEventListener("click", () => navigate("runs"));
    $("#switch-to-live-btn").addEventListener("click", () => {
      window.LOGS_MODE = { type: "live" };
      navigate("logs");   // re-init
    });
  },

  applyMode(mode) {
    if (mode.type === "history") {
      $("#logs-title").textContent = `Logs for run #${mode.runId}`;
      $("#logs-subtitle").textContent = "Historical — stored in the database";
      $("#back-to-runs-btn").style.display = "inline-flex";
      $("#switch-to-live-btn").style.display = "inline-flex";
    } else {
      $("#logs-title").textContent = "Live logs";
      $("#logs-subtitle").textContent = "Running monitor output (SSE)";
      $("#back-to-runs-btn").style.display = "none";
      $("#switch-to-live-btn").style.display = "none";
    }
  },

  render() {
    const box = $("#logs-box");
    if (!box) return;
    if (!LOG_BUFFER.length) {
      box.innerHTML = '<div class="muted">No log entries</div>';
      return;
    }
    // Single-line template — avoids extra whitespace
    box.innerHTML = LOG_BUFFER.map(l =>
      `<div class="log-line ${l.level || 'info'}"><span class="ts">${escapeHtml(l.ts || '')}</span><span class="msg">${escapeHtml(l.message || '')}</span></div>`
    ).join("");
    box.scrollTop = box.scrollHeight;
  },
};
