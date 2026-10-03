import { defineConfig, devices } from "@playwright/test";

/**
 * The M17 end-to-end suite (docs/plans/phase-2.md, M17 "Testing").
 *
 * Today it runs against the dev server, as two projects:
 * - `ui`: demo data (`npm run dev`), for accessibility checks (WCAG contrast measured on rendered
 *   pixels, reduced motion) that do not depend on the backend;
 * - `contract`: the real HTTP client (VITE_API_MODE=http) with /v1 intercepted by Playwright, asserting
 *   what the browser sends (session cookie mode, CSRF header, where a credential may appear).
 * When the M17 backend lands, set E2E_BASE_URL to a tenant host of the live stack
 * (http://<tenant>.edisc.localhost:5173 with VITE_API_MODE=http) and add the plan's flows
 * (dummy connection → collection → status → report → download, audit trail checked).
 */
const DEMO = "http://localhost:5173";
const HTTP = "http://localhost:5174";
const live = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { ...devices["Desktop Chrome"], viewport: { width: 1512, height: 945 }, trace: "retain-on-failure" },
  projects: [
    { name: "ui", testDir: "e2e/a11y", use: { baseURL: live ?? DEMO } },
    { name: "contract", testDir: "e2e/contract", use: { baseURL: HTTP } },
  ],
  webServer: [
    ...(live ? [] : [{ command: "npx vite --port 5173 --strictPort", url: DEMO, reuseExistingServer: !process.env.CI, timeout: 60_000 }]),
    {
      command: "npx vite --port 5174 --strictPort",
      url: HTTP,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      // Unroutable API origin: every /v1 call is answered by the test's route stubs, never a real server.
      env: { VITE_API_MODE: "http", EDISC_API_ORIGIN: "http://127.0.0.1:9" },
    },
  ],
});
