/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function apiProxy() {
  return {
    target: process.env.MACIP_API_ORIGIN ?? "http://127.0.0.1:8080",
    changeOrigin: false,
  };
}

// Desenvolvimento: mesma origem — /api encaminhado à API local.
// Produção: sirva dist/ e encaminhe /api/* pelo mesmo proxy (ver README).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": apiProxy() },
  },
  preview: {
    // `vite preview` serve o build e encaminha /api — espelho do dev
    proxy: { "/api": apiProxy() },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
