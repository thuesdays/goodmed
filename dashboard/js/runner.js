// ═══════════════════════════════════════════════════════════════
// runner.js — sidebar run widgets + SSE log stream
//
// Owns two pieces of sidebar UI:
//
//   1. "Run default profile" / "Stop default" button pair.
//      Starts the profile configured in browser.profile_name.
//      Stop button only fires when THAT profile is the one running.
//
//   2. Active-runs panel — appears when ≥1 slot is active, shows a row
//      per run with profile name, elapsed time, and a Stop button.
//      "Stop all" kills every active run.
//
// Both update every 3s by polling /api/run/status and /api/runs/active.
// ═══════════════════════════════════════════════════════════════

const runBtn     = () => document.getElementById("run-btn");
const stopBtn    = () => document.getElementById("stop-btn");
const runBtnText = () => document.getElementById("run-btn-text");
const runStatus  = () => document.getElementById("run-status");

// Global log buffer — shared with the Logs page. Lives for the whole
// dashboard session so we don't lose messages when switching pages.
const LOG_BUFFER = [];
const LOG_MAX    = 500;
const logSubscribers = [];

function onLogEntry(cb) {
  logSubscribers.push(cb);
  return () => {
    const i = logSubscribers.indexOf(cb);
    if (i >= 0) logSubscribers.splice(i, 1);
  };
}

function startLogStream() {
  const src = new EventSource("/api/logs/live");
  src.onmessage = (e) => {
    try {
      const entry = JSON.parse(e.data);
      LOG_BUFFER.push(entry);
      if (LOG_BUFFER.length > LOG_MAX) LOG_BUFFER.shift();
      logSubscribers.forEach(fn => { try { fn(entry); } catch {} });
    } catch {}
  };
  src.onerror = () => {
    try { src.close(); } catch {}
    setTimeout(startLogStream, 3000);
  };
}

// ─── Start/Stop for the sidebar's "default profile" button ──────

async function startRun(profileName) {
  try {
    const body = profileName ? JSON.stringify({ profile_name: profileName }) : "{}";
    await api("/api/run", { method: "POST", body });
    toast("✓ Monitor started");
    updateRunStatus();
  } catch (e) {
    toast("Error: " + e.message, true);
  }
}

async function stopRun() {
  const ok = await confirmDialog({
    title:        "Stop monitor",
    message:      "This stops the default profile's run. Other active runs are unaffected — use 'Stop all' to kill everything.",
    confirmText:  "Stop",
    cancelText:   "Keep running",
    confirmStyle: "danger",
  });
  if (!ok) return;

  try {
    const result = await api("/api/run/stop", { method: "POST" });
    const killed = (result && result.killed) || [];
    if (killed.length) {
      toast(`✓ Stopped (killed ${killed.length} process${killed.length !== 1 ? "es" : ""})`);
    } else {
      toast("✓ Stopped");
    }
    updateRunStatus();
  } catch (e) {
    toast("Error: " + e.message, true);
  }
}

async function stopSpecificRun(runId, profileName) {
  const ok = await confirmDialog({
    title:        "Stop run",
    message:      `Stop run #${runId} (profile: ${profileName})?\nOther active runs stay running.`,
    confirmText:  "Stop",
    confirmStyle: "danger",
  });
  if (!ok) return;
  try {
    await api(`/api/runs/${runId}/stop`, { method: "POST" });
    toast(`✓ Stopped #${runId}`);
    updateRunStatus();
  } catch (e) {
    toast("Error: " + e.message, true);
  }
}

async function stopAllRuns() {
  const ok = await confirmDialog({
    title:        "Stop all runs",
    message:      "Kill every active profile run, including their Chrome processes. This cannot be undone.",
    confirmText:  "Stop all",
    confirmStyle: "danger",
  });
  if (!ok) return;
  try {
    const r = await api("/api/runs/stop-all", { method: "POST" });
    toast(`✓ Stopped ${r.count || 0} run${r.count === 1 ? "" : "s"}`);
    updateRunStatus();
  } catch (e) {
    toast("Error: " + e.message, true);
  }
}

// ─── Status rendering ────────────────────────────────────────────

// Lightweight mm:ss formatter for "how long has this run been going"
function _elapsedShort(isoStr) {
  if (!isoStr) return "";
  const t = new Date(isoStr).getTime();
  if (isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m`;
  return `${m}m ${s % 60}s`;
}

async function updateRunStatus() {
  try {
    // Pull both endpoints in parallel — legacy default-profile state
    // and the full active-runs list.
    const [s, active] = await Promise.all([
      api("/api/run/status"),
      api("/api/runs/active"),
    ]);

    const activeRuns = active?.runs || [];
    const activeCount = activeRuns.length;

    // Default-profile control (sidebar Start/Stop pair). It shows Stop
    // ONLY if the active-run list contains the default profile name.
    const defaultProfile =
      (typeof configCache?.browser?.profile_name === "string")
        ? configCache.browser.profile_name
        : null;

    const defaultIsRunning = defaultProfile
      && activeRuns.some(r => r.profile_name === defaultProfile);

    if (defaultIsRunning) {
      runBtn().style.display = "none";
      stopBtn().style.display = "flex";
      const match = activeRuns.find(r => r.profile_name === defaultProfile);
      runStatus().textContent = `Running (#${match?.run_id || "?"})`;
    } else {
      runBtn().style.display = "flex";
      stopBtn().style.display = "none";
      if (s.finished_at && !activeCount) {
        const code = s.last_exit_code === 0 ? "ok" : `code ${s.last_exit_code}`;
        runStatus().textContent = `Finished (${code})`;
      } else {
        runStatus().textContent = activeCount
          ? `${activeCount} other run${activeCount === 1 ? "" : "s"} active`
          : "Ready";
      }
    }

    // Subtitle under the default button
    const sub = document.getElementById("run-status-sub");
    if (sub) {
      if (defaultIsRunning) {
        sub.textContent = `profile: ${defaultProfile}`;
      } else {
        sub.textContent = defaultProfile
          ? `default: ${defaultProfile}`
          : "no default profile set";
      }
    }

    // Active runs panel — show only if there's at least one run.
    // Lists every active slot independent of default-profile logic.
    _renderActiveRunsPanel(activeRuns);
  } catch {}
}

function _renderActiveRunsPanel(activeRuns) {
  const panel = document.getElementById("active-runs-panel");
  const list  = document.getElementById("active-runs-list");
  const cnt   = document.getElementById("active-runs-count-text");
  if (!panel || !list || !cnt) return;

  if (!activeRuns.length) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";
  cnt.textContent = `${activeRuns.length} running`;

  list.innerHTML = activeRuns
    .sort((a, b) => a.run_id - b.run_id)
    .map(r => {
      // Heartbeat visualisation. The backend returns heartbeat_age
      // (seconds since last DB ping from main.py). We classify:
      //   < 45s   → healthy (no badge, keep UI clean)
      //   45-90s  → amber "slow" pill (might be in long operation)
      //   > 90s   → red "WEDGED" pill (watchdog will kill soon, or
      //             it's between main.py's start and first heartbeat)
      let hbBadge = "";
      const hb = r.heartbeat_age;
      if (hb != null && hb >= 45) {
        const cls = hb >= 90 ? "active-run-badge wedged" : "active-run-badge slow";
        const label = hb >= 90
          ? `⚠ no ping ${Math.floor(hb)}s`
          : `slow ${Math.floor(hb)}s`;
        hbBadge = `<span class="${cls}" title="Seconds since last main.py heartbeat. Watchdog kills after 180s.">${label}</span>`;
      }
      return `
        <div class="active-run-row" data-run-id="${r.run_id}">
          <div class="active-run-info">
            <div class="active-run-name">
              ${escapeHtml(r.profile_name || "?")}
              ${hbBadge}
            </div>
            <div class="active-run-meta">
              #${r.run_id} · ${escapeHtml(_elapsedShort(r.started_at))}
            </div>
          </div>
          <button class="active-run-stop"
                  onclick="stopSpecificRun(${r.run_id}, '${escapeHtml(r.profile_name || "")}')"
                  title="Stop this run">■</button>
        </div>
      `;
    }).join("");
}

// Init
document.addEventListener("DOMContentLoaded", () => {
  runBtn().addEventListener("click", () => startRun());
  stopBtn().addEventListener("click", () => stopRun());

  const stopAllBtn = document.getElementById("btn-stop-all");
  if (stopAllBtn) stopAllBtn.addEventListener("click", () => stopAllRuns());

  setInterval(updateRunStatus, 3000);
  updateRunStatus();
  startLogStream();
});
