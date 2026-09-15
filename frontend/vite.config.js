import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to dist/ which the FastAPI backend serves as static
// files in production. In dev, `npm run dev` proxies /api to the backend
// so the same relative fetch("/api/...") calls work in both modes.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
