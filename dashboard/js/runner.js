// ═══════════════════════════════════════════════════════════════
// runner.js — sidebar active-runs panel + SSE log stream
//
// Owns one piece of sidebar UI:
//
//   • Active-runs panel — appears when ≥1 slot is active, shows a row
//     per run with profile name, elapsed time, and a Stop button.
//     "Stop all" kills every active run.
//
// History: this file used to also own a "Run default profile" /
// "Stop default" button pair in the sidebar footer. That was removed
// 2026-04 because all profile launching now flows through the
// Profiles page (per-row Start + bulk-start). Keeping the global
// active-runs panel + Stop-all is still valuable: it lets users see
// what's running from any page and bail out without navigating.
//
// The panel polls /api/runs/active + /api/scheduler/status every 3s
// for liveness. The SSE log stream is independent and used by the
// Logs page.
// ═══════════════════════════════════════════════════════════════

// Global log buffer — shared with the Logs page. Lives for the whole
// dashboard session so we don't lose messages when switching pages.
// Backed by a server-side ring buffer (see /api/logs/recent) so a page
// reload fills in with the last ~2000 lines instead of showing empty.
const LOG_BUFFER = [];
const LOG_MAX    = 2000;
const logSubscribers = [];

// Highest seq we've seen from the server. Used on SSE reconnect to
// backfill any messages we missed during the disconnect window.
let _lastLogSeq = 0;

function onLogEntry(cb) {
  logSubscribers.push(cb);
  return () => {
    const i = logSubscribers.indexOf(cb);
    if (i >= 0) logSubscribers.splice(i, 1);
  };
}

// System-event subscribers — separate from logSubscribers because
// events (like "run_finished") are structured signals, not text
// for the log viewer. The Overview page uses this to refresh stats
// immediately on run completion instead of polling every 15s.
const eventSubscribers = [];

/** Subscribe to named system events broadcast over the SSE channel.
 *  Returns an unsubscribe fn. Called by any page that wants
 *  live invalidation without polling.
 */
function onSystemEvent(eventName, cb) {
  const wrapper = (e) => { if (e.event === eventName) cb(e); };
  eventSubscribers.push(wrapper);
  return () => {
    const i = eventSubscribers.indexOf(wrapper);
    if (i >= 0) eventSubscribers.splice(i, 1);
  };
}

/** Internal — push entry to buffer, trim, track seq, notify subs. */
function _pushLogEntry(entry) {
  if (typeof entry.seq === "number" && entry.seq > _lastLogSeq) {
    _lastLogSeq = entry.seq;
  }
  // Demultiplex: structured events go to eventSubscribers only,
  // actual log lines go to logSubscribers and the buffer.
  if (entry.type === "event") {
    eventSubscribers.forEach(fn => { try { fn(entry); } catch {} });
    return;
  }
  LOG_BUFFER.push(entry);
  if (LOG_BUFFER.length > LOG_MAX) LOG_BUFFER.shift();
  logSubscribers.forEach(fn => { try { fn(entry); } catch {} });
}

/** Pull the ring-buffer from the server and prime LOG_BUFFER.
 *  Called once on boot and again whenever SSE reconnects. On reconnect
 *  we send ?since_seq so we only get the gap.
 */
async function primeLogBuffer() {
  try {
    const url = _lastLogSeq > 0
      ? `/api/logs/recent?since_seq=${_lastLogSeq}`
      : `/api/logs/recent?limit=2000`;
    const r = await fetch(url);
    if (!r.ok) return;
    const data = await r.json();
    const entries = data.entries || [];
    for (const e of entries) _pushLogEntry(e);
  } catch {
    // Silent — if /api/logs/recent is down, SSE will still work for
    // future messages. No point spamming errors.
  }
}

function startLogStream() {
  // Prime the buffer FIRST so by the time the page renders, the user
  // sees the last 2000 lines of history rather than empty.
  primeLogBuffer();

  const src = new EventSource("/api/logs/live");
  src.onmessage = (e) => {
    try {
      _pushLogEntry(JSON.parse(e.data));
    } catch {}
  };
  src.onerror = () => {
    try { src.close(); } catch {}
    setTimeout(startLogStream, 3000);
  };
}

// ─── Stop helpers ───────────────────────────────────────────────
// Exposed globally because the active-runs panel renders inline
// onclick handlers that call into them. (The panel rebuilds on every
// status poll, so a delegated listener would need extra plumbing for
// no real win.)

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
    // Two endpoints in parallel — full active-runs list AND scheduler
    // status. The scheduler badge gets painted onto the sidebar
    // Scheduler nav item below.
    const [active, sched] = await Promise.all([
      api("/api/runs/active"),
      api("/api/scheduler/status").catch(() => ({ is_running: false })),
    ]);

    const activeRuns = active?.runs || [];

    // Active runs panel — show only if there's at least one run.
    _renderActiveRunsPanel(activeRuns);

    // Scheduler badge on the sidebar nav item — subtle dot on the
    // Scheduler menu entry that turns green when scheduler is alive.
    _paintSchedulerNavBadge(sched);
  } catch {}
}

function _paintSchedulerNavBadge(sched) {
  // Find the Scheduler sidebar entry and toggle a dot on it. Keeps
  // the user aware the scheduler is active even when they're on a
  // different page.
  const navItem = document.querySelector('.sidebar-item[data-page="scheduler"]');
  if (!navItem) return;
  let dot = navItem.querySelector(".sidebar-nav-dot");
  const health = sched?.health || (sched?.is_running ? "ok" : "stopped");
  if (health === "stopped") {
    if (dot) dot.remove();
    return;
  }
  if (!dot) {
    dot = document.createElement("span");
    dot.className = "sidebar-nav-dot";
    dot.title     = "Scheduler is running";
    navItem.appendChild(dot);
  }
  // Colour: ok=green, stale=amber, crashed=red.
  dot.dataset.health = health;
  dot.title = {
    ok:      "Scheduler is running",
    stale:   "Scheduler wedged — no recent heartbeat",
    crashed: "Scheduler crashed — use 🧹 Clean zombies",
  }[health] || "Scheduler is running";
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
  const stopAllBtn = document.getElementById("btn-stop-all");
  if (stopAllBtn) stopAllBtn.addEventListener("click", () => stopAllRuns());

  setInterval(updateRunStatus, 3000);
  updateRunStatus();
  startLogStream();
});
