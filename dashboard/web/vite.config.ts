import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

// `npm run dev` proxies the API and the twin to a running `python -m dashboard --dry-run`.
export default defineConfig({
  plugins: [preact()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false, target: "es2022" },
  server: { proxy: { "/api": "http://127.0.0.1:8080", "/twin": "http://127.0.0.1:8080" } },
});
