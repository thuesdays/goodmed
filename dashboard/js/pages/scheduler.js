// ═══════════════════════════════════════════════════════════════
// scheduler.js — Scheduler page.
//
// Redesigned for multi-profile selection (search + bulk actions),
// group-as-cards, and 3-mode schedule picker (Simple / Interval /
// Cron). Cron mode gets a live "next N runs" preview computed
// client-side — same algorithm as ghost_shell/scheduler/cron.py,
// reimplemented in JS to avoid a round-trip per keystroke.
// ═══════════════════════════════════════════════════════════════

const Scheduler = (() => {

  let pollTimer = null;

  const state = {
    profiles:       [],
    profileFilter:  "",
    groups:         [],
    selectedGroup:  "",        // config value, "" = none
    mode:           "density",
    activeDays:     [1,2,3,4,5,6,7],
  };

  // ─────────────────────────────────────────────────────────────
  // init / teardown
  // ─────────────────────────────────────────────────────────────
  async function init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));
    bindEvents();

    // Hydrate state from config
    state.mode       = (configCache?.scheduler?.schedule_mode || "density");
    state.activeDays = Array.isArray(configCache?.scheduler?.active_days) &&
                       configCache.scheduler.active_days.length
                         ? configCache.scheduler.active_days.slice()
                         : [1,2,3,4,5,6,7];

    await Promise.all([loadProfiles(), loadGroups(), refresh()]);
    renderModeTabs();
    renderDaysChips();
    renderCronPreview();
    renderIntervalHuman();

    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (currentPage === "scheduler") refresh();
      else clearInterval(pollTimer);
    }, 4000);
  }

  function teardown() { clearInterval(pollTimer); }

  // ─────────────────────────────────────────────────────────────
  // Wiring
  // ─────────────────────────────────────────────────────────────
  function bindEvents() {
    $("#sched-start-btn").addEventListener("click", start);
    $("#sched-stop-btn").addEventListener("click", () => stop());
    _wireForceKillModifier();   // Shift+Click stop -> force kill
    $("#sched-refresh-btn").addEventListener("click", refresh);
    $("#sched-reap-btn")?.addEventListener("click", reapZombies);

    // Profile filter + bulk
    $("#sched-profile-search").addEventListener("input", (e) => {
      state.profileFilter = (e.target.value || "").toLowerCase();
      renderProfiles();
    });
    $$(".sched-bulk button, #sched-bulk-all, #sched-bulk-none, #sched-bulk-invert, #sched-bulk-healthy")
      .forEach(b => b.addEventListener("click", () => bulkAction(b.dataset.bulk)));

    // Mode tabs
    $("#sched-mode-tabs").addEventListener("click", (e) => {
      const t = e.target.closest(".sched-mode-tab");
      if (!t) return;
      setMode(t.dataset.mode);
    });

    // Cron input → live preview + error display
    $("#sched-cron-expr").addEventListener("input", () => {
      // data-config-binding auto-saves; we just re-render preview
      renderCronPreview();
    });
    $$(".sched-cron-preset").forEach(b => b.addEventListener("click", (e) => {
      e.preventDefault();
      const inp = $("#sched-cron-expr");
      inp.value = b.dataset.cron;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
    }));

    // Interval input → human summary
    $("#sched-interval-sec").addEventListener("input", renderIntervalHuman);
  }

  // ─────────────────────────────────────────────────────────────
  // Profile list
  // ─────────────────────────────────────────────────────────────
  async function loadProfiles() {
    try {
      state.profiles = await api("/api/profiles");
      renderProfiles();
    } catch (e) { console.error("loadProfiles:", e); }
  }

  function selectedProfileNames() {
    return new Set(configCache?.scheduler?.profile_names || []);
  }

  function saveProfileSelection(names) {
    configCache.scheduler = configCache.scheduler || {};
    configCache.scheduler.profile_names = names;
    scheduleConfigSave();
    updateProfileCount();
  }

  function updateProfileCount() {
    const n = (configCache?.scheduler?.profile_names || []).length;
    $("#sched-profile-count").textContent = n;
  }

  function renderProfiles() {
    const list = $("#sched-profiles-list");
    const selected = selectedProfileNames();
    const needle = state.profileFilter;

    if (!state.profiles.length) {
      list.innerHTML = '<div class="dense-empty" style="grid-column:1/-1;">No profiles yet — create one on the Profiles page.</div>';
      updateProfileCount();
      return;
    }

    const filtered = needle
      ? state.profiles.filter(p =>
          (p.name || "").toLowerCase().includes(needle) ||
          (p.status || "").toLowerCase().includes(needle) ||
          (p.tags || []).some(t => (t || "").toLowerCase().includes(needle)))
      : state.profiles;

    if (!filtered.length) {
      list.innerHTML = `<div class="dense-empty" style="grid-column:1/-1;">No profiles match "${escapeHtml(needle)}".</div>`;
      return;
    }

    list.innerHTML = filtered.map(p => {
      const on  = selected.has(p.name);
      const status = p.status || "ready";
      const blocks = p.consecutive_blocks || 0;
      const healthCls = blocks >= 3 ? "bad" : blocks > 0 ? "warn" : "ok";
      const tagsHtml = (p.tags || []).slice(0, 3).map(t =>
        `<span class="sched-profile-tag">${escapeHtml(t)}</span>`).join("");
      return `
        <label class="sched-profile-card ${on ? "checked" : ""}">
          <input type="checkbox" data-profile="${escapeHtml(p.name)}"
                 ${on ? "checked" : ""}>
          <div class="sched-profile-card-body">
            <div class="sched-profile-card-name">
              <span class="sched-profile-dot sched-dot-${healthCls}"></span>
              <strong>${escapeHtml(p.name)}</strong>
            </div>
            <div class="sched-profile-card-meta">
              <span>${escapeHtml(status)}</span>
              ${blocks > 0 ? `<span class="sched-profile-blocks">⚠ ${blocks} blocks</span>` : ""}
            </div>
            ${tagsHtml ? `<div class="sched-profile-card-tags">${tagsHtml}</div>` : ""}
          </div>
        </label>
      `;
    }).join("");
    updateProfileCount();

    // Wire checkbox changes
    list.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", () => {
        // Rebuild the full selection set from current checkbox state,
        // including profiles that are hidden by the filter (we pick
        // them up from the cached selection).
        const visibleNames = Array.from(list.querySelectorAll("input[type=checkbox]"))
          .map(c => c.dataset.profile);
        const newlyChecked = Array.from(list.querySelectorAll("input[type=checkbox]:checked"))
          .map(c => c.dataset.profile);
        const hidden = Array.from(selectedProfileNames())
          .filter(n => !visibleNames.includes(n));
        const next = Array.from(new Set([...hidden, ...newlyChecked]));
        saveProfileSelection(next);
        // Update the checked-class on the label
        cb.closest(".sched-profile-card").classList.toggle("checked", cb.checked);
      });
    });
  }

  function bulkAction(kind) {
    if (!state.profiles.length) return;
    let names = new Set(selectedProfileNames());
    const visible = state.profiles.filter(p => {
      if (!state.profileFilter) return true;
      const n = state.profileFilter;
      return (p.name || "").toLowerCase().includes(n) ||
             (p.status || "").toLowerCase().includes(n) ||
             (p.tags || []).some(t => (t || "").toLowerCase().includes(n));
    });
    if (kind === "all") {
      for (const p of visible) names.add(p.name);
    } else if (kind === "none") {
      for (const p of visible) names.delete(p.name);
    } else if (kind === "invert") {
      for (const p of visible) {
        if (names.has(p.name)) names.delete(p.name);
        else                    names.add(p.name);
      }
    } else if (kind === "healthy") {
      names = new Set();
      for (const p of visible) {
        if ((p.consecutive_blocks || 0) === 0) names.add(p.name);
      }
    }
    saveProfileSelection(Array.from(names));
    renderProfiles();
  }

  // ─────────────────────────────────────────────────────────────
  // Groups — card picker
  // ─────────────────────────────────────────────────────────────
  async function loadGroups() {
    try {
      state.groups = await api("/api/groups");
      state.selectedGroup = String(configCache?.scheduler?.group_id || "");
      renderGroups();
    } catch (e) { console.warn("loadGroups:", e); }
  }

  function renderGroups() {
    const host = $("#sched-group-list");
    const cards = [
      // "None" card first — explicitly re-selectable
      `<label class="sched-group-card ${state.selectedGroup === "" ? "selected" : ""}">
        <input type="radio" name="sched-group" value="" ${state.selectedGroup === "" ? "checked" : ""}>
        <div class="sched-group-card-title">— None —</div>
        <div class="sched-group-card-desc">
          Cycle through the profiles selected above, one per iteration.
        </div>
      </label>`,
      ...state.groups.map(g => `
        <label class="sched-group-card ${state.selectedGroup === String(g.id) ? "selected" : ""}">
          <input type="radio" name="sched-group" value="${g.id}"
                 ${state.selectedGroup === String(g.id) ? "checked" : ""}>
          <div class="sched-group-card-title">📁 ${escapeHtml(g.name)}</div>
          <div class="sched-group-card-meta">
            ${g.member_count} member${g.member_count === 1 ? "" : "s"}
          </div>
          <div class="sched-group-card-desc">
            ${escapeHtml(g.description || "—")}
          </div>
        </label>
      `),
    ];
    host.innerHTML = cards.join("");

    host.querySelectorAll('input[name="sched-group"]').forEach(r => {
      r.addEventListener("change", (e) => {
        state.selectedGroup = e.target.value;
        configCache.scheduler = configCache.scheduler || {};
        configCache.scheduler.group_id = e.target.value || null;
        scheduleConfigSave();
        renderGroups();
        $("#sched-group-mode-row").style.display =
          state.selectedGroup ? "block" : "none";
      });
    });

    $("#sched-group-mode-row").style.display =
      state.selectedGroup ? "block" : "none";
  }

  // ─────────────────────────────────────────────────────────────
  // Schedule mode tabs
  // ─────────────────────────────────────────────────────────────
  function setMode(mode) {
    state.mode = mode;
    configCache.scheduler = configCache.scheduler || {};
    configCache.scheduler.schedule_mode = mode;
    scheduleConfigSave();
    renderModeTabs();
  }

  function renderModeTabs() {
    $$(".sched-mode-tab").forEach(t =>
      t.classList.toggle("active", t.dataset.mode === state.mode));
    $$(".sched-mode-pane").forEach(p =>
      p.style.display = p.dataset.modePane === state.mode ? "block" : "none");
  }

  // ─────────────────────────────────────────────────────────────
  // Active-days chips (Mon-Sun toggles)
  // ─────────────────────────────────────────────────────────────
  function renderDaysChips() {
    const host = $("#sched-days-chips");
    const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    host.innerHTML = names.map((n, i) => {
      const dow = i + 1;   // ISO 1..7
      const on = state.activeDays.includes(dow);
      return `
        <button class="sched-day-chip ${on ? "on" : ""}" data-dow="${dow}">
          ${n}
        </button>`;
    }).join("");
    host.querySelectorAll("[data-dow]").forEach(b => {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        const dow = parseInt(b.dataset.dow, 10);
        if (state.activeDays.includes(dow)) {
          state.activeDays = state.activeDays.filter(x => x !== dow);
        } else {
          state.activeDays = [...state.activeDays, dow].sort();
        }
        // Empty = treat as "every day" (same as backend)
        configCache.scheduler = configCache.scheduler || {};
        configCache.scheduler.active_days =
          state.activeDays.length === 7 ? [] : state.activeDays;
        scheduleConfigSave();
        renderDaysChips();
      });
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Interval human summary
  // ─────────────────────────────────────────────────────────────
  function renderIntervalHuman() {
    const el = $("#sched-interval-human");
    if (!el) return;
    const sec = parseInt($("#sched-interval-sec")?.value, 10) || 0;
    if (!sec) { el.textContent = "—"; return; }
    const perDay = Math.floor(86400 / sec);
    let human;
    if (sec < 60)        human = `every ${sec}s`;
    else if (sec < 3600) human = `every ${(sec / 60).toFixed(1).replace(/\.0$/, "")} min`;
    else                 human = `every ${(sec / 3600).toFixed(1).replace(/\.0$/, "")}h`;
    el.textContent = `${human} · ~${perDay} runs/day if window is 24h`;
  }

  // ─────────────────────────────────────────────────────────────
  // Cron preview — mini JS-side parser matching ghost_shell.scheduler.cron
  // ─────────────────────────────────────────────────────────────
  function parseField(raw, lo, hi) {
    const out = new Set();
    for (const partRaw of raw.split(",")) {
      const part = partRaw.trim();
      if (!part) throw new Error("empty part");
      let step = 1, base = part;
      if (part.includes("/")) { [base, step] = part.split("/"); step = parseInt(step, 10); }
      if (!step || step <= 0) throw new Error("bad step");
      let start, end;
      if (base === "*") { start = lo; end = hi; }
      else if (base.includes("-")) {
        const [a, b] = base.split("-").map(x => parseInt(x, 10));
        if (isNaN(a) || isNaN(b)) throw new Error("bad range");
        start = a; end = b;
      } else {
        const n = parseInt(base, 10);
        if (isNaN(n)) throw new Error(`bad value: ${base}`);
        start = end = n;
      }
      if (start < lo || end > hi || start > end)
        throw new Error(`out of bounds ${start}-${end}`);
      for (let v = start; v <= end; v += step) out.add(v);
    }
    return out;
  }

  function cronMatches(dt, sets) {
    const [mi, hr, dm, mo, dw] = sets;
    // JS: Sun=0..Sat=6; cron: Sun=0..Sat=6 — aligned
    return mi.has(dt.getMinutes()) &&
           hr.has(dt.getHours()) &&
           dm.has(dt.getDate()) &&
           mo.has(dt.getMonth() + 1) &&
           dw.has(dt.getDay());
  }

  function cronNextN(expr, n = 5, start = new Date()) {
    const fields = expr.trim().split(/\s+/);
    if (fields.length !== 5) throw new Error(`expected 5 fields, got ${fields.length}`);
    const bounds = [[0,59], [0,23], [1,31], [1,12], [0,6]];
    const sets = fields.map((f, i) => parseField(f, bounds[i][0], bounds[i][1]));
    const dt = new Date(start);
    dt.setSeconds(0, 0);
    dt.setMinutes(dt.getMinutes() + 1);
    const horizon = new Date(dt.getTime() + 366 * 86400 * 1000);
    const out = [];
    while (out.length < n && dt < horizon) {
      if (cronMatches(dt, sets)) {
        out.push(new Date(dt));
      }
      dt.setMinutes(dt.getMinutes() + 1);
    }
    return out;
  }

  function renderCronPreview() {
    const expr = ($("#sched-cron-expr")?.value || "").trim();
    const err  = $("#sched-cron-error");
    const list = $("#sched-cron-preview-list");
    err.style.display = "none";
    if (!expr) {
      list.innerHTML = '<li class="muted">Type an expression to see preview…</li>';
      return;
    }
    try {
      const next = cronNextN(expr, 5);
      if (!next.length) {
        list.innerHTML = '<li class="muted">No matches in the next year.</li>';
        return;
      }
      list.innerHTML = next.map(d => {
        const day = d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
        const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return `<li><span class="muted">${day}</span> · <strong>${time}</strong></li>`;
      }).join("");
    } catch (e) {
      err.textContent = `Invalid expression: ${e.message}`;
      err.style.display = "block";
      list.innerHTML = '<li class="muted">—</li>';
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Status + lifecycle (unchanged behaviour from old scheduler.js)
  // ─────────────────────────────────────────────────────────────
  async function refresh() {
    try {
      const s = await api("/api/scheduler/status");
      renderStatus(s);
    } catch (e) { console.error("scheduler status:", e); }
  }

  function renderStatus(s) {
    const running = s.is_running;
    const health  = s.health || (running ? "ok" : "stopped");
    // Show Stop whenever there is *any* live process — including a wedged
    // ("stale": pid alive, no heartbeat) scheduler. Otherwise the user
    // sees only Start while a zombie scheduler eats CPU and has no way
    // to kill it from the UI. Falls back to is_running when pid field
    // is absent (older server build).
    const hasProc = !!(s.pid && s.pid > 0) || running;
    const startBtn = $("#sched-start-btn");
    const stopBtn  = $("#sched-stop-btn");
    if (startBtn) startBtn.style.display = hasProc ? "none" : "inline-flex";
    if (stopBtn)  stopBtn.style.display  = hasProc ? "inline-flex" : "none";

    // If we just transitioned IN to running state, restore the Stop
    // button label/disabled in case a previous click left it stuck.
    if (hasProc && stopBtn && stopBtn.dataset._busy !== "1") {
      stopBtn.disabled = false;
      stopBtn.innerHTML = "■ Stop scheduler";
    }
    if (!hasProc && startBtn && startBtn.dataset._busy !== "1") {
      startBtn.disabled = false;
      startBtn.innerHTML = "▶ Start scheduler";
    }

    const labelByHealth = {
      ok: "Running", stale: "Wedged — no heartbeat",
      crashed: "Crashed (stale state)", stopped: "Stopped",
    };
    const colorByHealth = {
      ok: "var(--healthy)", stale: "var(--warning, #f59e0b)",
      crashed: "var(--danger, #ef4444)", stopped: "var(--text-muted)",
    };
    $("#sched-status-value").textContent = labelByHealth[health] || "—";
    $("#sched-status-value").style.color = colorByHealth[health];

    const pill = $("#sched-health-pill");
    if (pill) {
      if (health === "ok") { pill.style.display = "none"; }
      else {
        pill.style.display = "inline-block";
        pill.textContent   = health;
        pill.className     = `health-pill health-pill-${health}`;
      }
    }

    if (running) {
      const since = s.started_at ? timeAgo(s.started_at) : "—";
      let sub = `since ${since}`;
      if (s.heartbeat_age != null && s.heartbeat_age >= 0) {
        sub += ` · ${s.heartbeat_age < 60
          ? `ping ${s.heartbeat_age}s ago`
          : `⚠ no ping ${Math.floor(s.heartbeat_age / 60)}m`}`;
      }
      $("#sched-status-sub").textContent = sub;
    } else if (health === "crashed") {
      $("#sched-status-sub").textContent = "last heartbeat was too old — click 🧹 Clean zombies";
    } else {
      $("#sched-status-sub").textContent = "idle";
    }

    $("#sched-runs-today").textContent  = s.runs_today ?? 0;
    $("#sched-runs-target").textContent = `of ${s.target_runs_per_day ?? "—"}`;

    if (s.next_run_at) {
      const d = new Date(s.next_run_at);
      $("#sched-next-run").textContent =
        d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const diffSec = (d - new Date()) / 1000;
      $("#sched-next-in").textContent =
        diffSec < 0   ? "overdue" :
        diffSec < 60  ? `in ${Math.floor(diffSec)}s` :
        diffSec < 3600? `in ${Math.floor(diffSec / 60)}m` :
                        `in ${Math.floor(diffSec / 3600)}h`;
    } else {
      $("#sched-next-run").textContent = "—";
      $("#sched-next-in").textContent  = "—";
    }
    $("#sched-last-profile").textContent = s.last_run_profile || "—";
  }

  async function reapZombies() {
    const btn = $("#sched-reap-btn");
    const orig = btn?.textContent || "";
    if (btn) { btn.disabled = true; btn.textContent = "🧹 Cleaning…"; }
    try {
      const r = await fetch("/api/admin/reap-zombies", { method: "POST" });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
      const bits = [];
      if (body.runs_killed)      bits.push(`killed ${body.runs_killed} wedged`);
      if (body.runs_marked_dead) bits.push(`marked ${body.runs_marked_dead} dead`);
      if (body.runs_left_alive)  bits.push(`${body.runs_left_alive} still alive`);
      toast(bits.length ? `Cleaned: ${bits.join(", ")}` : "Nothing to clean — all good ✓");
      refresh();
    } catch (e) { toast(`Reap failed: ${e.message}`, true); }
    finally { if (btn) { btn.disabled = false; btn.textContent = orig; } }
  }

  async function start() {
    const btn = $("#sched-start-btn");
    btn.dataset._busy = "1";
    btn.disabled = true; btn.textContent = "Starting...";
    // Optimistic flip: hide Start, show Stop immediately so the user
    // sees the action register without waiting for the next status poll.
    btn.style.display = "none";
    const stopBtn = $("#sched-stop-btn");
    if (stopBtn) {
      stopBtn.style.display = "inline-flex";
      stopBtn.disabled = false;
      stopBtn.innerHTML = "■ Stop scheduler";
    }
    try {
      await api("/api/scheduler/start", { method: "POST" });
      toast("✓ Scheduler started");
      await refresh();
    } catch (e) {
      toast("Error: " + e.message, true);
      // Roll back the optimistic flip on failure.
      btn.style.display = "inline-flex";
      if (stopBtn) stopBtn.style.display = "none";
    }
    finally {
      btn.dataset._busy = "0";
      btn.disabled = false;
      btn.innerHTML = "▶ Start scheduler";
    }
  }

  async function stop(opts = {}) {
    const force = !!opts.force;
    const btn = $("#sched-stop-btn");
    if (!btn) return;

    // Confirm only on the soft path. Force-kill is an explicit choice
    // and shouldn't double-prompt.
    if (!force) {
      const ok = await confirmDialog({
        title: "Stop scheduler",
        message: "The scheduler will stop now. If a tick is mid-flight, " +
                 "any running browser instances will be terminated with it.",
        confirmText: "Stop scheduler",
        confirmStyle: "warning",
      });
      if (!ok) return;
    }

    btn.dataset._busy = "1";
    btn.disabled = true; btn.textContent = force ? "Force killing..." : "Stopping...";
    // Optimistic flip: swap to Start *before* the server call returns.
    btn.style.display = "none";
    const startBtn = $("#sched-start-btn");
    if (startBtn) {
      startBtn.style.display = "inline-flex";
      startBtn.disabled = true;
      startBtn.innerHTML = force ? "Force killing..." : "Stopping...";
    }

    const url = force
      ? "/api/scheduler/stop?force=1"
      : "/api/scheduler/stop";
    try {
      const r = await api(url, { method: "POST" });
      if (r && r.warning) {
        toast(`Stopped (with warning): ${r.warning}`, true);
      } else if (r && r.already_stopped) {
        toast("Scheduler was already stopped — UI synced");
      } else {
        toast(force ? "✓ Scheduler force-killed" : "✓ Scheduler stopped");
      }
      await refresh();
    } catch (e) {
      toast("Error: " + e.message, true);
      // Don't roll back the flip — the user clicked Stop, they
      // expect the UI to show Start now. Refresh will reconcile.
      await refresh();
    } finally {
      btn.dataset._busy = "0";
      btn.disabled = false;
      btn.innerHTML = "■ Stop scheduler";
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.innerHTML = "▶ Start scheduler";
      }
    }
  }

  // Shift-click on Stop = force-kill (skip terminate(), straight to kill())
  // Useful when the scheduler is wedged inside a 1800s sleep loop and
  // ignores SIGTERM. Wired in init() below.
  function _wireForceKillModifier() {
    const btn = document.getElementById("sched-stop-btn");
    if (!btn || btn.dataset._wiredForce === "1") return;
    btn.dataset._wiredForce = "1";
    btn.addEventListener("click", (e) => {
      if (e.shiftKey) {
        e.preventDefault();
        e.stopImmediatePropagation();
        stop({ force: true });
      }
    }, true);  // capture phase so we beat the regular click handler
    btn.title = "Click: stop · Shift+Click: force-kill";
  }

  return { init, teardown };
})();
