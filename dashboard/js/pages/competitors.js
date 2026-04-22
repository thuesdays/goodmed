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

      this.renderByDomain(data.by_domain || []);
      this.renderRecent(data.recent || []);
    } catch (e) {
      console.error(e);
    }
  },

  renderByDomain(domains) {
    const tbody = $("#competitors-tbody");
    if (!domains.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No competitors found yet</td></tr>`;
      return;
    }
    tbody.innerHTML = domains.map(d => `
      <tr>
        <td><strong>${escapeHtml(d.domain)}</strong></td>
        <td>${d.mentions ?? d.count ?? 0}</td>
        <td class="muted">${escapeHtml((d.queries || []).join(" · "))}</td>
        <td class="muted">${escapeHtml(d.first_seen || "—")}</td>
        <td class="muted">${escapeHtml(d.last_seen || "—")}</td>
      </tr>
    `).join("");
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
