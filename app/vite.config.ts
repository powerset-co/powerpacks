import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import { powerpacksLocalApiPlugin } from "./local-api/powerpacksLocalApiPlugin";

export default defineConfig(() => ({
  server: {
    host: process.env.HOST || "127.0.0.1",
    port: 5177,
    strictPort: false,
    watch: {
      ignored: [
        "**/.powerpacks/**",
        "**/.codex/**",
        "**/.venv/**",
        "**/node_modules/**",
        "**/dist/**",
      ],
    },
  },
  plugins: [
    react(),
    powerpacksLocalApiPlugin(),
  ].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    commonjsOptions: {
      ignoreTryCatch: false,
    },
  },
}));
