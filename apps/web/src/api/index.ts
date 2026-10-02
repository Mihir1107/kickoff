import type { ApiClient } from "./client";
import { createHttpClient } from "./http";

/**
 * The single switch between the real API and demo data.
 *
 * Demo mode exists only in the dev server (`npm run dev`, default unless VITE_API_MODE=http). In a
 * production build `import.meta.env.DEV` is false, so the demo branch and its dynamic import are
 * removed; vite.config.ts also fails the build if VITE_API_MODE=demo or if any demo module reaches
 * the bundle.
 */
const DEMO = import.meta.env.DEV && import.meta.env.VITE_API_MODE !== "http";
export const API_MODE: "demo" | "http" = DEMO ? "demo" : "http";

/** Session expired or missing: send the user to sign in again (wired by the router in main.tsx). */
let unauthenticated = () => {};
export const onUnauthenticated = (fn: () => void) => {
  unauthenticated = fn;
};

export const api: ApiClient = DEMO
  ? (await import("./demo")).createDemoClient()
  : createHttpClient({ onUnauthenticated: () => unauthenticated() });

export * from "./client";
export type * from "./types";
