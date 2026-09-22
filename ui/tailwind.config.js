// Colors are CSS variables (space-separated RGB) set per theme in ui/src/app.css,
// so one compiled stylesheet serves every theme, including the live Omarchy palette.
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

module.exports = {
  content: ["./ui/templates/**/*.html", "./ui/static/js/**/*.js"],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      bg: token("bg"),
      "bg-deep": token("bg-deep"),
      surface: token("surface"),
      line: token("line"),
      fg: token("fg"),
      muted: token("muted"),
      accent: token("accent"),
      accent2: token("accent2"),
      ok: token("ok"),
      warn: token("warn"),
      bad: token("bad"),
      "on-accent": token("on-accent"),
    },
    fontFamily: {
      sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      mono: ["JetBrainsMono Nerd Font", "JetBrains Mono", "ui-monospace", "monospace"],
    },
    extend: {
      borderRadius: { xl2: "1.25rem" },
    },
  },
  plugins: [],
};
