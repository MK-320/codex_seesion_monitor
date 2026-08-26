import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendOrigin = "http://127.0.0.1:8766";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": {
        target: backendOrigin,
        changeOrigin: true,
        configure(proxy) {
          proxy.on("proxyReq", (proxyRequest) => {
            proxyRequest.setHeader("origin", backendOrigin);
          });
        },
      },
      "/ws": {
        target: backendOrigin,
        ws: true,
        changeOrigin: true,
        rewriteWsOrigin: true,
      },
    },
  },
});
