import path from "node:path"
import react from "@vitejs/plugin-react-swc"
import { defineConfig } from "vitest/config"

const PYTHON_SERVER = process.env.PYTHON_SERVER ?? "http://127.0.0.1:8765"
// The paths the Python server answers: /api, and the form routes the review page posts to.
const PYTHON_ROUTES = ["/api", "/worth", "/complete", "/retarget", "/feedback", "/auth"]

export default defineConfig({
  base: "./",
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    proxy: Object.fromEntries(PYTHON_ROUTES.map((route) => [route, PYTHON_SERVER])),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: { app: "src/main.tsx" },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
  },
})
