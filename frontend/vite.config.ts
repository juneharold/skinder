import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8742",
        changeOrigin: true,
        configure(proxy) {
          proxy.on("proxyReq", (proxyRequest, request) => {
            // Satisfy the backend's origin check for our dev pages without accepting foreign origins.
            if (
              ["http://127.0.0.1:5174", "http://localhost:5174"].includes(
                request.headers.origin ?? "",
              )
            ) {
              proxyRequest.setHeader("Origin", "http://127.0.0.1:8742");
            }
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
