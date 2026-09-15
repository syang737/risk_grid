import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // The API host is configurable so a dev machine can point at a different
  // instance without editing this file.
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: Number(env.VITE_PORT ?? 5173),
      proxy: { "/api": { target, changeOrigin: true } },
    },
  };
});
