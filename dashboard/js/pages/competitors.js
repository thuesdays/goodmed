// ═══════════════════════════════════════════════════════════════
// pages/competitors.js
// ═══════════════════════════════════════════════════════════════

const Competitors = {
  async init() {
    try {
      const data = await api("/api/competitors");

      $("#comp-total").textContent = data.total_records;
      $("#comp-domains").textContent = data.unique_domains;
      $("#badge-competitors").textContent = data.unique_domains;

      // Aggregate actions totals across all domains for summary card
      const by_domain = data.by_domain || [];
      let totalRan = 0, totalSkipped = 0, totalErrored = 0;
      for (const d of by_domain) {
        totalRan     += d.actions_ran     || 0;
        totalSkipped += d.actions_skipped || 0;
        totalErrored += d.actions_errored || 0;
      }
      const actionsEl = $("#comp-actions");
      if (actionsEl) actionsEl.textContent = totalRan;
      const subEl = $("#comp-actions-sub");
      if (subEl) {
        const parts = [];
        if (totalSkipped) parts.push(`${totalSkipped} skipped`);
        if (totalErrored) parts.push(`${totalErrored} errored`);
        subEl.textContent = parts.length ? parts.join(" · ") : "no skipped / errored";
      }

      this.renderByDomain(by_domain);
      this.renderRecent(data.recent || []);
    } catch (e) {
      console.error(e);
    }
  },

  renderByDomain(domains) {
    const tbody = $("#competitors-tbody");
    if (!domains.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No competitors found yet</td></tr>`;
      return;
    }
    tbody.innerHTML = domains.map(d => {
      const ran   = d.actions_ran     || 0;
      const skipd = d.actions_skipped || 0;
      const err   = d.actions_errored || 0;

      // Build the actions cell: bold green number for ran, small grey
      // detail for skipped/errored underneath. "—" if nothing happened.
      let actionsCell;
      if (ran === 0 && skipd === 0 && err === 0) {
        actionsCell = `<span class="muted">—</span>`;
      } else {
        const subs = [];
        if (skipd) subs.push(`${skipd} skipped`);
        if (err)   subs.push(`${err} err`);
        actionsCell = `
          <div class="actions-cell">
            <strong>${ran}</strong>
            ${subs.length
              ? `<div class="actions-sub">${subs.join(" · ")}</div>`
              : ""}
          </div>`;
      }

      return `
        <tr>
          <td><strong>${escapeHtml(d.domain)}</strong></td>
          <td>${d.mentions ?? d.count ?? 0}</td>
          <td>${actionsCell}</td>
          <td class="muted">${escapeHtml((d.queries || []).join(" · "))}</td>
          <td class="muted">${escapeHtml(d.first_seen || "—")}</td>
          <td class="muted">${escapeHtml(d.last_seen || "—")}</td>
        </tr>`;
    }).join("");
  },

  renderRecent(recent) {
    // Reverse so newest is first
    const rows = recent.slice().reverse();
    $("#recent-badge").textContent = rows.length;

    const tbody = $("#recent-tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No data</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(r => `
      <tr>
        <td class="muted">${escapeHtml(r.timestamp)}</td>
        <td>${escapeHtml(r.query)}</td>
        <td><strong>${escapeHtml(r.domain)}</strong></td>
        <td>
          <a href="${escapeHtml(r.google_click_url || '#')}" target="_blank"
             class="muted" style="font-size: 11px;">
            ${escapeHtml((r.google_click_url || "").substring(0, 90))}${(r.google_click_url || "").length > 90 ? "…" : ""}
          </a>
        </td>
      </tr>
    `).join("");
  },
};
