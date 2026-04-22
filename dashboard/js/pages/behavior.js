// ═══════════════════════════════════════════════════════════════
// pages/behavior.js
// ═══════════════════════════════════════════════════════════════

const Behavior = {
  // Default timing values — must match db.py DEFAULT_CONFIG
  DEFAULTS: {
    "behavior.initial_load_min":     2.0,
    "behavior.initial_load_max":     4.0,
    "behavior.serp_settle_min":      1.5,
    "behavior.serp_settle_max":      3.0,
    "behavior.post_refresh_min":     2.0,
    "behavior.post_refresh_max":     4.0,
    "behavior.post_rotate_min":      2.0,
    "behavior.post_rotate_max":      4.0,
    "behavior.fresh_google_min":     3.0,
    "behavior.fresh_google_max":     5.0,
    "behavior.post_consent_min":     2.0,
    "behavior.post_consent_max":     4.0,
    "behavior.between_queries_min":  6.0,
    "behavior.between_queries_max": 12.0,
    "search.refresh_max_attempts":   4,
    "search.refresh_min_sec":       10,
    "search.refresh_max_sec":       15,
  },

  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    const btn = document.getElementById("reset-timings-btn");
    if (btn) btn.addEventListener("click", () => this.resetToDefaults());
  },

  async resetToDefaults() {
    if (!await confirmDialog({
      title: "Reset all timing values?",
      message: "Every delay, wait, and retry value on this page will go " +
        "back to the defaults that Ghost Shell ships with. Your action " +
        "pipelines and naturalness toggles are NOT affected.\n\n" +
        "Proceed?",
      confirmText: "Reset",
      confirmStyle: "danger",
    })) return;

    const btn = document.getElementById("reset-timings-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Resetting…";

    try {
      // Save each default via the config API
      const writes = Object.entries(this.DEFAULTS).map(([k, v]) =>
        api("/api/config", {
          method: "POST",
          body: JSON.stringify({ key: k, value: v }),
        })
      );
      await Promise.all(writes);

      // Reload config cache and re-bind inputs so UI reflects new values
      await loadConfig(true);
      bindConfigInputs($("#content"));
      toast("✓ Timings reset to defaults");
    } catch (e) {
      toast("Reset failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "↺ Reset timings to defaults";
    }
  },
};
