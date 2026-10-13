import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0c10",
        panel: "#0f1218",
        panel2: "#141922",
        border: "#1f2733",
        border2: "#2a3442",
        txt: "#d6dde8",
        muted: "#7d8a9c",
        profit: "#22c55e",
        loss: "#ef4444",
        warn: "#f59e0b",
        info: "#3b82f6",
        live: "#dc2626",
        paper: "#3b82f6",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
