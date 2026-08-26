import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "CODEX_");
  const backendPort = env.CODEX_MONITOR_PORT || "8766";
  return {
    base: "/static/",
    plugins: [react()],
    build: {
      outDir: "../src/codex_monitor/static",
      emptyOutDir: true,
    },
    server: {
      host: "0.0.0.0",
      port: Number(env.CODEX_MONITOR_VITE_PORT || "5173"),
      strictPort: true,
      allowedHosts: ["terminal.local"],
      proxy: {
        "/api": `http://127.0.0.1:${backendPort}`,
        "/ws": { target: `ws://127.0.0.1:${backendPort}`, ws: true },
      },
    },
  };
});
