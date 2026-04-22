// ═══════════════════════════════════════════════════════════════
// utils.js — HTML escape, JSON pretty, date helpers
// ═══════════════════════════════════════════════════════════════

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtJson(obj) {
  if (obj == null) return "<em>null</em>";
  const json = JSON.stringify(obj, null, 2);
  return json
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(\.\d+)?)/g,
      m => {
        let cls = "num";
        if (/^"/.test(m)) cls = /:$/.test(m) ? "key" : "str";
        else if (/true|false|null/.test(m)) cls = "bool";
        return `<span class="${cls}">${escapeHtml(m)}</span>`;
      });
}

function fmtDuration(startedAt, finishedAt) {
  if (!startedAt) return "—";
  if (!finishedAt) return null;
  const diff = (new Date(finishedAt) - new Date(startedAt)) / 1000;
  if (diff > 3600) return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
  if (diff > 60)   return `${Math.floor(diff / 60)}m ${Math.floor(diff % 60)}s`;
  return `${Math.floor(diff)}s`;
}

function fmtTimestamp(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ");
}

// ─── Custom modal confirm dialog ───────────────────────────────
// Returns a Promise that resolves with true/false.
// Usage:
//   if (await confirmDialog("Stop the current run?")) { ... }
//   await confirmDialog({
//     title: "Delete profile",
//     message: "All data will be permanently lost.",
//     confirmText: "Delete",
//     confirmStyle: "danger",
//   });

function confirmDialog(opts) {
  if (typeof opts === "string") opts = { message: opts };
  const {
    title        = "Confirm",
    message      = "",
    confirmText  = "Yes",
    cancelText   = "Cancel",
    confirmStyle = "primary",   // "primary" | "danger" | "warning"
  } = opts || {};

  return new Promise(resolve => {
    // Remove any existing modal first
    document.querySelectorAll(".gs-modal-backdrop").forEach(n => n.remove());

    const backdrop = document.createElement("div");
    backdrop.className = "gs-modal-backdrop";
    backdrop.innerHTML = `
      <div class="gs-modal">
        <div class="gs-modal-header">${escapeHtml(title)}</div>
        <div class="gs-modal-body">${escapeHtml(message).replace(/\n/g, "<br>")}</div>
        <div class="gs-modal-actions">
          <button class="gs-modal-btn gs-modal-btn-cancel">${escapeHtml(cancelText)}</button>
          <button class="gs-modal-btn gs-modal-btn-confirm gs-modal-btn-${confirmStyle}">
            ${escapeHtml(confirmText)}
          </button>
        </div>
      </div>
    `;

    const close = (result) => {
      backdrop.classList.add("gs-modal-closing");
      setTimeout(() => {
        backdrop.remove();
        document.removeEventListener("keydown", onKey);
      }, 150);
      resolve(result);
    };

    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter")  close(true);
    };

    backdrop.addEventListener("click", e => {
      if (e.target === backdrop) close(false);
    });
    backdrop.querySelector(".gs-modal-btn-cancel").addEventListener("click", () => close(false));
    backdrop.querySelector(".gs-modal-btn-confirm").addEventListener("click", () => close(true));
    document.addEventListener("keydown", onKey);

    document.body.appendChild(backdrop);
    // Focus confirm button so Enter works and a Tab jumps to Cancel
    setTimeout(() => backdrop.querySelector(".gs-modal-btn-confirm").focus(), 50);
  });
}
