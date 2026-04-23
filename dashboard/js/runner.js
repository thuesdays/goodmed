// ═══════════════════════════════════════════════════════════════
// runner.js — start/stop button + status polling + GLOBAL SSE logs
// ═══════════════════════════════════════════════════════════════

const runBtn     = () => $("#run-btn");
const stopBtn    = () => $("#stop-btn");
const runBtnText = () => $("#run-btn-text");
const runStatus  = () => $("#run-status");

// Global log buffer — shared with the Logs page. Lives for the whole
// dashboard session so we don't lose messages when switching pages.
const LOG_BUFFER = [];
const LOG_MAX    = 500;
const logSubscribers = [];   // callbacks (entry) => void

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
    setTimeout(startLogStream, 3000);    // auto-reconnect
  };
}

// ─── Start/Stop buttons ─────────────────────────────────────────

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
    message:      "This will forcefully kill the browser and its subprocesses.\nCurrent run will be marked as failed.",
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

async function updateRunStatus() {
  try {
    const s = await api("/api/run/status");
    if (s.is_running) {
      runBtn().style.display = "none";
      stopBtn().style.display = "flex";
      runStatus().textContent = `Running (#${s.current_run_id || "?"})`;
      const sub = document.getElementById("run-status-sub");
      if (sub) sub.textContent = s.profile_name ? `profile: ${s.profile_name}` : "";
    } else {
      runBtn().style.display = "flex";
      stopBtn().style.display = "none";
      if (s.finished_at) {
        const code = s.last_exit_code === 0 ? "ok" : `code ${s.last_exit_code}`;
        runStatus().textContent = `Finished (${code})`;
      } else {
        runStatus().textContent = "Ready";
      }
      // Show which profile the sidebar button would launch
      const sub = document.getElementById("run-status-sub");
      if (sub) {
        const defaultProfile =
          (typeof configCache?.browser?.profile_name === "string")
            ? configCache.browser.profile_name
            : null;
        sub.textContent = defaultProfile
          ? `default: ${defaultProfile}`
          : "no default profile set";
      }
    }
  } catch {}
}

// Init
document.addEventListener("DOMContentLoaded", () => {
  runBtn().addEventListener("click", () => startRun());
  stopBtn().addEventListener("click", () => stopRun());
  setInterval(updateRunStatus, 3000);
  updateRunStatus();
  startLogStream();
});
