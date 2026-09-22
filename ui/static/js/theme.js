/* Theme switching shared by the studio and the browser lock preview. */
(function () {
  const THEMES = ["omarchy", "midnight", "matrix", "sunrise"];
  const root = document.documentElement;

  function markSwatches(theme) {
    document.querySelectorAll("[data-theme-pick]").forEach((el) => {
      el.setAttribute("aria-pressed", String(el.dataset.themePick === theme));
    });
  }

  async function applyTheme(theme, persist) {
    if (!THEMES.includes(theme)) return;
    if (theme === "omarchy" && !root.dataset.omarchy) {
      // Page was rendered without the desktop palette; fetch it now.
      try {
        const res = await fetch("/api/theme");
        const data = await res.json();
        if (!data.available) return window.toast?.("No Omarchy theme found", "warn");
        const style = document.createElement("style");
        style.textContent = `[data-theme="omarchy"] { ${data.omarchy_vars} }`;
        document.head.appendChild(style);
        root.dataset.omarchy = "1";
      } catch {
        return;
      }
    }
    root.dataset.theme = theme;
    markSwatches(theme);
    if (persist) {
      fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme }),
      }).catch(() => {});
    }
  }

  document.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-theme-pick]");
    if (pick) applyTheme(pick.dataset.themePick, true);
  });

  document.addEventListener("DOMContentLoaded", () => markSwatches(root.dataset.theme));
  window.applyTheme = applyTheme;
})();
