import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL("../", import.meta.url)), "TALOS_");
  const apiTarget = `http://127.0.0.1:${env.TALOS_PORT || "8000"}`;

  return {
    plugins: [react(), tailwindcss()],
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": apiTarget,
        "/health": apiTarget,
        "/docs": apiTarget,
        "/openapi.json": apiTarget,
      },
    },
  };
});
