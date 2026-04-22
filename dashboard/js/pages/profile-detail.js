// ═══════════════════════════════════════════════════════════════
// pages/profile-detail.js
// ═══════════════════════════════════════════════════════════════

const ProfileDetail = {
  currentProfile: null,

  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // Populate profile selector
    await this.populateSelector();

    $("#profile-selector").addEventListener("change", (e) => {
      this.currentProfile = e.target.value;
      this.loadSelfcheck(this.currentProfile);
      this.loadFingerprint(this.currentProfile);
    });

    $("#reset-health-btn").addEventListener("click", () => this.resetHealth());
    $("#clear-history-btn").addEventListener("click", () => this.clearHistory());
    $("#delete-profile-btn").addEventListener("click", () => this.deleteProfile());

    const regenBtn = document.getElementById("regen-fp-btn");
    if (regenBtn) {
      regenBtn.addEventListener("click", () => this.regenerateFingerprint());
    }

    // Pre-load for current active profile
    this.currentProfile = configCache?.browser?.profile_name;
    if (this.currentProfile) {
      $("#profile-selector").value = this.currentProfile;
      await Promise.all([
        this.loadSelfcheck(this.currentProfile),
        this.loadFingerprint(this.currentProfile),
      ]);
    }
  },

  async populateSelector() {
    try {
      const profiles = await api("/api/profiles");
      const select = $("#profile-selector");
      select.innerHTML = profiles
        .map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`)
        .join("");
    } catch (e) {
      console.error(e);
    }
  },

  async loadSelfcheck(name) {
    try {
      const sc = await api(`/api/profiles/${encodeURIComponent(name)}/selfcheck`);
      $("#selfcheck-badge").textContent = `${sc.passed}/${sc.total}`;
      $("#selfcheck-time").textContent = `Last check: ${sc.timestamp}`;

      const tests = sc.tests || {};
      const items = Object.entries(tests).map(([testName, result]) => {
        const ok = result === true;
        return `
          <div class="selfcheck-item ${ok ? 'pass' : 'fail'}">
            <span class="icon">${ok ? '✓' : '✗'}</span>
            <span>${escapeHtml(testName)}</span>
          </div>
        `;
      }).join("");
      $("#selfcheck-grid").innerHTML = items || '<div class="empty-state">No data</div>';
    } catch (e) {
      $("#selfcheck-badge").textContent = "—";
      $("#selfcheck-grid").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
    }
  },

  async loadFingerprint(name) {
    try {
      const fp = await api(`/api/profiles/${encodeURIComponent(name)}/fingerprint`);
      $("#fingerprint-view").innerHTML = fmtJson(fp);
    } catch (e) {
      $("#fingerprint-view").innerHTML = `<span class="muted">${escapeHtml(e.message)}</span>`;
    }
  },

  async resetHealth() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Reset health counter",
      message: `Reset consecutive blocks counter for "${this.currentProfile}"?`,
      confirmText: "Reset",
    })) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/reset-health`,
                { method: "POST" });
      toast("✓ Blocks counter reset");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async clearHistory() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Clear history",
      message: `Clear ALL session quality history for "${this.currentProfile}"?\nThis cannot be undone.`,
      confirmText: "Clear",
      confirmStyle: "warning",
    })) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}/clear-history`,
                { method: "POST" });
      toast("✓ History cleared");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async deleteProfile() {
    if (!this.currentProfile) return;
    if (!await confirmDialog({
      title: "Delete profile",
      message:
        `Delete profile "${this.currentProfile}"?\n\n` +
        `This removes the profile folder AND purges all related DB rows ` +
        `(events, fingerprints, self-checks). Run history is kept.\n\n` +
        `This cannot be undone.`,
      confirmText: "Delete profile",
      confirmStyle: "danger",
    })) return;

    try {
      await api(`/api/profiles/${encodeURIComponent(this.currentProfile)}`,
                { method: "DELETE" });
      toast(`✓ Deleted "${this.currentProfile}"`);
      // Navigate away
      navigate("profiles");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async regenerateFingerprint() {
    if (!this.currentProfile) {
      toast("No profile selected", true);
      return;
    }
    if (!await confirmDialog({
      title: "🎲 Regenerate fingerprint?",
      message: `The fingerprint for "${this.currentProfile}" will be ` +
        `replaced with a freshly-generated one (new UA, screen, GPU, fonts, etc.). ` +
        `The self-check cache will be cleared. The profile's user-data-dir ` +
        `(cookies, history) is NOT touched.\n\n` +
        `Use this when the current fingerprint is getting flagged.`,
      confirmText: "Regenerate",
      confirmStyle: "primary",
    })) return;

    const btn = document.getElementById("regen-fp-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ Rolling…";
    }

    try {
      const r = await api(
        `/api/profiles/${encodeURIComponent(this.currentProfile)}`
        + `/regenerate-fingerprint`,
        { method: "POST", body: JSON.stringify({}) }
      );
      if (r.ok) {
        toast(`✓ New fingerprint: ${r.template} (Chrome ${r.chrome_version})`);
        await this.loadFingerprint(this.currentProfile);
      } else {
        toast(r.error || "regeneration failed", true);
      }
    } catch (e) {
      toast(e.message || "regeneration failed", true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🎲 Regenerate fingerprint";
      }
    }
  },
};
