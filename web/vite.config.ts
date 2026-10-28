import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: { charts: ["recharts"] },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/v1": "http://127.0.0.1:8789",
      "/v2": "http://127.0.0.1:8789",
      "/auth": "http://127.0.0.1:8789",
    },
  },
});
