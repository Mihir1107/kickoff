import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

const DEMO_DIR = "/src/api/demo/";

/**
 * Demo data is dev-only. A production build refuses VITE_API_MODE=demo outright, and fails if any
 * demo module still reaches a chunk (belt and braces: api/index.ts guards the import with
 * import.meta.env.DEV, which is false in every build).
 */
function noDemoInBuild(mode: string): Plugin {
  return {
    name: "edisc:no-demo-in-build",
    apply: "build",
    config() {
      const value = loadEnv(mode, process.cwd(), "VITE_").VITE_API_MODE ?? process.env.VITE_API_MODE;
      if (value === "demo") {
        throw new Error("VITE_API_MODE=demo is dev-only: a production build must use the real API (unset it or set VITE_API_MODE=http).");
      }
      if (value !== undefined && value !== "http") throw new Error(`Unknown VITE_API_MODE=${value} (expected "http" in a build).`);
    },
    generateBundle(_, bundle) {
      for (const chunk of Object.values(bundle)) {
        const leaked = chunk.type === "chunk" ? chunk.moduleIds.filter((id) => id.includes(DEMO_DIR)) : [];
        if (leaked.length) this.error(`demo data leaked into the production bundle (${chunk.fileName}): ${leaked.join(", ")}`);
      }
    },
  };
}

// The API resolves the tenant from the Host subdomain (ADR 0013), so in dev the SPA is served at
// http://<tenant>.edisc.localhost:5173 and /v1 is proxied same-origin with the Host header kept
// (changeOrigin: false). Same origin also means the session cookie needs no CORS or SameSite=None.
export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), noDemoInBuild(mode)],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: { target: "es2022" }, // api/index.ts uses top-level await to keep demo code out of builds
  server: {
    host: true,
    allowedHosts: [".edisc.localhost", "localhost"],
    proxy: {
      "/v1": { target: process.env.EDISC_API_ORIGIN ?? "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
}));
