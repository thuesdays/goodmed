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

// ─── Byte size formatting ────────────────────────────────────────
// Canonical helper shared across pages — especially the Traffic page
// and the Overview traffic card. Kept here so any future page that
// needs byte formatting can just call `formatBytes(N)` without
// re-implementing the ladder.
//
// Examples:
//   formatBytes(0)         → "0 B"
//   formatBytes(512)       → "512 B"
//   formatBytes(1536)      → "1.5 KB"
//   formatBytes(1572864)   → "1.5 MB"
//   formatBytes(2.5 * 1e9) → "2.33 GB"
//
// We stop at TB — anyone moving more than that is exceptional and
// should probably look at the raw byte count in the API response.
function formatBytes(n, precision = 1) {
  n = Number(n) || 0;
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  // Round to `precision` decimals, strip trailing zeros for readability
  return `${n.toFixed(precision).replace(/\.0+$/, "")} ${units[i]}`;
}
// Also expose on window so inline <script> snippets and late-loaded
// modules can find it without worrying about script load order.
window.formatBytes = formatBytes;

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

/**
 * Format an ISO timestamp as a relative-to-now string.
 *   "3s ago" · "2m ago" · "1h ago" · "yesterday" · "5d ago" · ISO for older.
 * Returns "—" for falsy input.
 */
function timeAgo(iso) {
  if (!iso) return "—";
  const then = new Date(iso);
  if (isNaN(then)) return iso;
  const s = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (s < 5)     return "just now";
  if (s < 60)   return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 2)  return "yesterday";
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}d ago`;
  return then.toISOString().slice(0, 10);   // old enough, just show date
}
