// ═══════════════════════════════════════════════════════════════
// config-form.js — shared config load/save (autosave with debounce)
// ═══════════════════════════════════════════════════════════════

// Global config cache — pages read from it, then call saveConfig()
let configCache = null;
let configSaveTimeout = null;

async function loadConfig() {
  try {
    configCache = await api("/api/config");
    return configCache;
  } catch (e) {
    toast("Config load error: " + e.message, true);
    return null;
  }
}

async function saveConfig() {
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify(configCache),
    });
    toast("✓ Saved");
  } catch (e) {
    toast("Error: " + e.message, true);
  }
}

function scheduleConfigSave() {
  clearTimeout(configSaveTimeout);
  configSaveTimeout = setTimeout(saveConfig, 800);
}

// Bind input/change to autosave using data-config="path.to.key" attribute.
// Example: <input data-config="proxy.url">  — will auto-update configCache
// and schedule save when user types / toggles / selects.
function bindConfigInputs(container) {
  const root = container || document;
  root.querySelectorAll("[data-config]").forEach(el => {
    const path = el.dataset.config;
    // Populate
    const value = getByPath(configCache, path);
    if (el.type === "checkbox") {
      el.checked = !!value;
    } else if (el.tagName === "TEXTAREA" && el.dataset.configList === "true") {
      el.value = (Array.isArray(value) ? value : []).join("\n");
    } else {
      el.value = value ?? "";
    }

    // Bind change
    const handler = () => {
      let newValue;
      if (el.type === "checkbox") {
        newValue = el.checked;
      } else if (el.tagName === "TEXTAREA" && el.dataset.configList === "true") {
        newValue = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      } else if (el.type === "number") {
        newValue = el.value === "" ? null : parseFloat(el.value);
      } else {
        newValue = el.value;
      }
      setByPath(configCache, path, newValue);
      scheduleConfigSave();
    };

    el.addEventListener(el.type === "checkbox" ? "change" : "input", handler);
  });
}

function getByPath(obj, path) {
  if (!obj) return undefined;
  return path.split(".").reduce((o, k) => (o ? o[k] : undefined), obj);
}

function setByPath(obj, path, value) {
  const keys = path.split(".");
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (cur[keys[i]] == null || typeof cur[keys[i]] !== "object") {
      cur[keys[i]] = {};
    }
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = value;
}
