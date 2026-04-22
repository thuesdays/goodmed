// ═══════════════════════════════════════════════════════════════
// pages/runs.js — history + mark-failed + view-logs
// ═══════════════════════════════════════════════════════════════

const Runs = {
  async init() {
    $("#reload-runs-btn").addEventListener("click", () => this.reload());
    $("#btn-clear-runs").addEventListener("click", () => this.clearRuns());
    await this.reload();
  },

  async clearRuns() {
    const scope = $("#clear-runs-scope").value;
    const isAll = scope === "all";
    const days  = isAll ? null : parseInt(scope, 10);

    const title   = isAll
      ? "🗑 Clear ALL run history?"
      : `🗑 Clear runs older than ${days} days?`;
    const message = isAll
      ? "This permanently removes every run record. You'll lose all " +
        "historical metrics. Profile fingerprints and config are kept.\n\n" +
        "This cannot be undone."
      : `Run records older than ${days} day(s) will be permanently deleted. ` +
        `This frees up DB space but means you won't be able to review them.`;

    if (!await confirmDialog({
      title, message,
      confirmText: "Clear",
      confirmStyle: "danger",
    })) return;

    const btn = $("#btn-clear-runs");
    btn.disabled = true;
    btn.textContent = "⏳ Clearing…";

    try {
      const r = await api("/api/runs/clear", {
        method: "POST",
        body: JSON.stringify(days == null ? {} : { older_than_days: days }),
      });
      if (r.ok) {
        toast(`✓ Deleted ${r.deleted} run(s)`);
        await this.reload();
      } else {
        toast(r.error || "clear failed", true);
      }
    } catch (e) {
      toast(e.message || "clear failed", true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Clear";
    }
  },

  async reload() {
    try {
      const runs = await api("/api/runs");

      $("#runs-total").textContent = runs.length;
      $("#runs-success").textContent = runs.filter(r => r.exit_code === 0).length;
      $("#runs-failed").textContent = runs.filter(r => r.exit_code != null && r.exit_code !== 0).length;

      const tbody = $("#runs-tbody");
      if (!runs.length) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No runs yet</td></tr>`;
        return;
      }

      tbody.innerHTML = runs.map(r => this.renderRow(r)).join("");
    } catch (e) {
      console.error(e);
    }
  },

  renderRow(r) {
    const started = r.started_at ? r.started_at.replace("T", " ") : "—";
    const duration = fmtDuration(r.started_at, r.finished_at)
      || '<span class="pill pill-running">running</span>';

    let exitBadge;
    if (r.exit_code === 0) {
      exitBadge = '<span class="pill pill-healthy">OK</span>';
    } else if (r.exit_code == null) {
      exitBadge = '<span class="pill pill-idle">—</span>';
    } else {
      exitBadge = `<span class="pill pill-critical">${r.exit_code}</span>`;
    }

    const stuck = r.finished_at == null && r.exit_code == null;

    const actions = [
      `<button class="btn-sm" onclick="Runs.viewLogs(${r.id})">View logs</button>`,
    ];
    if (stuck) {
      actions.push(
        `<button class="btn-sm btn-danger" onclick="Runs.markFailed(${r.id})">Mark failed</button>`
      );
    }

    return `
      <tr>
        <td><strong>#${r.id}</strong></td>
        <td class="muted">${escapeHtml(started)}</td>
        <td>${escapeHtml(r.profile_name || "—")}</td>
        <td>${duration}</td>
        <td>${r.total_queries || 0}</td>
        <td>${r.total_ads || 0}</td>
        <td>${r.captchas || 0}</td>
        <td>${exitBadge}</td>
        <td><div class="btn-group">${actions.join("")}</div></td>
      </tr>
    `;
  },

  async viewLogs(runId) {
    try {
      const logs = await api(`/api/logs/history?run_id=${runId}&limit=500`);
      LOG_BUFFER.length = 0;
      logs.reverse().forEach(l => {
        LOG_BUFFER.push({
          ts:      (l.timestamp || "").substring(11, 19),
          level:   l.level || "info",
          message: l.message || "",
        });
      });
      // Tell the logs page we're in history mode
      window.LOGS_MODE = { type: "history", runId };
      toast(`Loaded ${logs.length} log entries for run #${runId}`);
      navigate("logs");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async markFailed(runId) {
    const ok = await confirmDialog({
      title: `Mark run #${runId} as failed`,
      message:
        `This sets exit_code=-99 and finished_at=now.\n\n` +
        `Use this to clean up stuck "running" entries when you know ` +
        `the process is dead.`,
      confirmText: "Mark failed",
      confirmStyle: "warning",
    });
    if (!ok) return;
    try {
      await api(`/api/runs/${runId}/mark-failed`, { method: "POST" });
      toast(`✓ Run #${runId} marked as failed`);
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },
};
