import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  prefix: "",
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        border: "var(--border)",
        input: "var(--input)",
        ring: "var(--primary)",
        card: { DEFAULT: "var(--card)", foreground: "var(--foreground)" },
        popover: { DEFAULT: "var(--card)", foreground: "var(--foreground)" },
        primary: {
          DEFAULT: "var(--primary)",
          foreground: "white",
          action: "var(--primary-action)",
          hover: "var(--primary-hover)",
          soft: "var(--primary-soft)",
        },
        secondary: { DEFAULT: "var(--secondary)", foreground: "var(--foreground)" },
        muted: { DEFAULT: "var(--secondary)", foreground: "var(--muted)" },
        accent: { DEFAULT: "var(--accent)", foreground: "var(--foreground)" },
        destructive: { DEFAULT: "var(--bad)", foreground: "var(--foreground)" },
        "surface-2": "var(--surface-2)",
        line: { DEFAULT: "var(--line)", strong: "var(--line-strong)" },
        faint: "var(--faint)",
        ok: { DEFAULT: "var(--ok)", soft: "var(--ok-soft)" },
        warn: { DEFAULT: "var(--warn)", soft: "var(--warn-soft)" },
        bad: { DEFAULT: "var(--bad)", soft: "var(--bad-soft)" },
        info: { DEFAULT: "var(--info)", soft: "var(--info-soft)" },
        error: "var(--error)",
      },
      borderRadius: {
        sm: "var(--radius-s)",
        md: "var(--radius-m)",
        lg: "var(--radius-l)",
      },
      boxShadow: {
        1: "var(--shadow-1)",
        2: "var(--shadow-2)",
      },
      transitionDuration: {
        fast: "var(--t-fast)",
        med: "var(--t-med)",
        exit: "var(--t-exit)",
      },
      transitionTimingFunction: {
        out: "var(--ease-out)",
        in: "var(--ease-in)",
      },
      height: {
        topbar: "var(--topbar-height)",
        row: "var(--row-h)",
      },
      spacing: {
        topbar: "var(--topbar-height)",
        row: "var(--row-h)",
      },
    },
  },
  plugins: [],
} satisfies Config;
