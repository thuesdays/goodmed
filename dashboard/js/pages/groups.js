// ═══════════════════════════════════════════════════════════════
// pages/groups.js — Profile Groups (batch operations)
//
// A group is a named bag of profile names with optional group-wide
// settings (max_parallel cap). The user picks a group and clicks
// "▶ Start group" — the backend launches each member in its own
// concurrent slot up to the cap, queueing the overflow.
//
// Single-page-level state: live view of group list + modal for
// create/edit. Runs view (which member is active) comes from the
// sidebar's active-runs panel, not duplicated here.
// ═══════════════════════════════════════════════════════════════

const Groups = {
  groups:        [],
  allProfiles:   [],
  editingGroupId: null,    // null when creating, int when editing
  // Working members set for the edit modal — drag-out into real save
  // only when the user clicks Save, so Cancel is truly a no-op.
  _workingMembers: new Set(),

  async init() {
    document.getElementById("btn-create-group")
      ?.addEventListener("click", () => this.openCreate());
    document.getElementById("btn-reload-groups")
      ?.addEventListener("click", () => this.reload());
    document.getElementById("btn-save-group")
      ?.addEventListener("click", () => this.saveGroup());

    // Modal close wiring
    document.querySelectorAll('[data-close="group-edit-modal"]').forEach(el => {
      el.addEventListener("click", () => this.closeModal());
    });

    await this.reload();

    // Refresh periodically so "running members" counter stays live
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => this.refreshRunCounts(), 3000);
  },

  teardown() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  },

  async reload() {
    try {
      const [groups, profiles, active] = await Promise.all([
        api("/api/groups"),
        api("/api/profiles"),
        api("/api/runs/active"),
      ]);
      this.groups      = groups;
      this.allProfiles = profiles;
      this._runningSet = new Set((active.runs || []).map(r => r.profile_name));
      this.render();
    } catch (e) {
      toast("Failed to load groups: " + e.message, true);
    }
  },

  /** Refresh only the "N running" counters without rebuilding the whole
   *  list — keeps the UI calm when a group card is expanded. */
  async refreshRunCounts() {
    try {
      const active = await api("/api/runs/active");
      const newSet = new Set((active.runs || []).map(r => r.profile_name));
      // Only re-render if running membership actually changed
      const changed = newSet.size !== (this._runningSet?.size || 0)
        || [...newSet].some(n => !this._runningSet.has(n));
      this._runningSet = newSet;
      if (changed) this.render();
    } catch {}
  },

  render() {
    const container = document.getElementById("groups-list");
    if (!container) return;

    if (!this.groups.length) {
      container.innerHTML = `
        <div class="empty-state" style="padding: 40px 20px;">
          <div style="font-size: 14px; margin-bottom: 8px;">
            No groups yet.
          </div>
          <div class="muted" style="font-size: 12px;">
            Create your first group to batch-run multiple profiles with one click.
          </div>
        </div>`;
      return;
    }

    container.innerHTML = this.groups.map(g => {
      // How many of this group's members are currently running?
      // We don't have .members on list-view — use a separate API call
      // to get full details lazily when expanding. For the list card,
      // just show member_count and a rough running estimate.
      const running = this._runningMembersOf(g);
      const runChip = running > 0
        ? `<span class="group-running-chip">
             <span class="run-dot-small"></span> ${running} running
           </span>`
        : "";
      const maxP = g.max_parallel
        ? `cap ${g.max_parallel}`
        : "cap: global default";

      return `
        <div class="group-card" data-id="${g.id}">
          <div class="group-card-main">
            <div class="group-card-info">
              <div class="group-card-title">
                ${escapeHtml(g.name)}
                ${runChip}
              </div>
              <div class="group-card-meta">
                ${g.member_count} member${g.member_count === 1 ? "" : "s"} ·
                ${escapeHtml(maxP)}
                ${g.description
                  ? ` · <span class="muted">${escapeHtml(g.description)}</span>`
                  : ""}
              </div>
            </div>
            <div class="group-card-actions">
              <button class="btn btn-primary group-start-btn ${running > 0 ? "is-running" : ""}"
                      onclick="Groups.startGroup(${g.id})"
                      ${g.member_count === 0
                          ? "disabled title='No members'"
                          : (running > 0
                             ? `disabled title='Group already running (${running} member${running === 1 ? "" : "s"}). Stop the group before starting again.'`
                             : "")}>
                ▶ Start group
              </button>
              <button class="btn btn-danger-strong group-stop-btn"
                      onclick="Groups.stopGroup(${g.id})"
                      ${running === 0 ? "disabled" : ""}>
                ■ Stop${running > 0 ? ` (${running})` : ""}
              </button>
              <button class="btn btn-secondary"
                      onclick="Groups.openEdit(${g.id})">
                ✏ Edit
              </button>
              <button class="btn btn-secondary"
                      onclick="Groups.deleteGroup(${g.id})"
                      style="color: var(--critical)">
                🗑
              </button>
            </div>
          </div>
        </div>`;
    }).join("");
  },

  /** Approximate running count for a group — uses allProfiles' groups[]
   *  back-reference rather than re-fetching each group's full member list. */
  _runningMembersOf(group) {
    if (!this._runningSet?.size) return 0;
    // Find profiles whose groups[] include this group id
    let n = 0;
    for (const p of this.allProfiles) {
      const gids = (p.groups || []).map(g => g.id);
      if (gids.includes(group.id) && this._runningSet.has(p.name)) n++;
    }
    return n;
  },

  // ─── CREATE / EDIT ──────────────────────────────────────────

  openCreate() {
    this.editingGroupId = null;
    this._workingMembers = new Set();
    document.getElementById("group-modal-title").textContent = "✨ Create new group";
    document.getElementById("group-name-input").value  = "";
    document.getElementById("group-desc-input").value  = "";
    document.getElementById("group-maxp-input").value  = "";
    this._renderMembersPicker();
    document.getElementById("group-edit-modal").style.display = "flex";
    setTimeout(() => document.getElementById("group-name-input").focus(), 50);
  },

  async openEdit(groupId) {
    try {
      const g = await api(`/api/groups/${groupId}`);
      this.editingGroupId = groupId;
      this._workingMembers = new Set(g.members || []);
      document.getElementById("group-modal-title").textContent = `✏ Edit group: ${g.name}`;
      document.getElementById("group-name-input").value  = g.name || "";
      document.getElementById("group-desc-input").value  = g.description || "";
      document.getElementById("group-maxp-input").value  = g.max_parallel ?? "";
      this._renderMembersPicker();
      document.getElementById("group-edit-modal").style.display = "flex";
    } catch (e) {
      toast("Failed to load group: " + e.message, true);
    }
  },

  closeModal() {
    document.getElementById("group-edit-modal").style.display = "none";
  },

  _renderMembersPicker() {
    const picker = document.getElementById("group-members-picker");
    if (!picker) return;
    if (!this.allProfiles.length) {
      picker.innerHTML = `<div class="muted">No profiles exist yet — create some first.</div>`;
      return;
    }
    picker.innerHTML = this.allProfiles.map(p => {
      const checked = this._workingMembers.has(p.name) ? "checked" : "";
      const tagStr = (p.tags || []).slice(0, 3)
        .map(t => `<span class="profile-tag-chip">${escapeHtml(t)}</span>`)
        .join("");
      return `
        <label class="group-member-row">
          <input type="checkbox" data-name="${escapeHtml(p.name)}" ${checked}>
          <span class="group-member-name">${escapeHtml(p.name)}</span>
          <span class="group-member-tags">${tagStr}</span>
          <span class="group-member-status pill pill-${p.status}">${p.status}</span>
        </label>`;
    }).join("");

    picker.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", (e) => {
        const name = e.target.dataset.name;
        if (e.target.checked) this._workingMembers.add(name);
        else                  this._workingMembers.delete(name);
      });
    });
  },

  async saveGroup() {
    const name = (document.getElementById("group-name-input").value || "").trim();
    if (!name) {
      toast("Name is required", true);
      return;
    }
    const desc = (document.getElementById("group-desc-input").value || "").trim() || null;
    const maxPRaw = document.getElementById("group-maxp-input").value;
    const maxP = maxPRaw ? parseInt(maxPRaw, 10) : null;

    const payload = {
      name,
      description:  desc,
      max_parallel: (isNaN(maxP) || maxP <= 0) ? null : maxP,
      members:      Array.from(this._workingMembers),
    };

    try {
      if (this.editingGroupId == null) {
        await api("/api/groups", { method: "POST", body: JSON.stringify(payload) });
        toast(`✓ Created "${name}"`);
      } else {
        await api(`/api/groups/${this.editingGroupId}`, {
          method: "POST",
          body:   JSON.stringify(payload),
        });
        toast(`✓ Saved "${name}"`);
      }
      this.closeModal();
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async deleteGroup(groupId) {
    const g = this.groups.find(x => x.id === groupId);
    const ok = await confirmDialog({
      title:   "Delete group",
      message: `Delete group "${g?.name || "?"}"?\n\nProfiles themselves are NOT deleted — only the group and its membership list.`,
      confirmText:  "Delete",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      await api(`/api/groups/${groupId}`, { method: "DELETE" });
      toast("✓ Deleted");
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  // ─── START / STOP GROUP ─────────────────────────────────────

  async startGroup(groupId) {
    try {
      const r = await api(`/api/groups/${groupId}/start`, { method: "POST" });
      const started = (r.started || []).length;
      const queued  = (r.queued  || []).length;
      const errs    = (r.errors  || []).length;

      let msg = `✓ Started ${started}`;
      if (queued) msg += ` · ${queued} queued (cap: ${r.max_parallel})`;
      if (errs)   msg += ` · ${errs} errored`;
      toast(msg, errs > 0);

      // Refresh group card counters + sidebar run panel
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async stopGroup(groupId) {
    const ok = await confirmDialog({
      title:   "Stop group",
      message: "Stop every active run belonging to this group? Other groups' runs are unaffected.",
      confirmText:  "Stop",
      confirmStyle: "danger",
    });
    if (!ok) return;
    try {
      const r = await api(`/api/groups/${groupId}/stop`, { method: "POST" });
      toast(`✓ Stopped ${r.count || 0}`);
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },
};
