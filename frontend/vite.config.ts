/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the FastAPI static dir and committed, so Render's Python build needs no Node.
export default defineConfig({
  plugins: [react()],
  base: "/static/terminal/",
  build: { outDir: "../backend/static/terminal", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.ts"], globals: false },
});
