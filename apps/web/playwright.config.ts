/**
 * End-to-end journeys (T1, phase-8-plan.md "End-to-end").
 *
 * `pnpm e2e` boots scripts/dev_stack.py (the real cricai_api on in-memory
 * SQLite with the seeded demo lab, plus a PRODUCTION build served by `next start`,
 * the same thing the lab tablet runs) on its own ports, then runs
 * every journey on a tablet and a desktop viewport. The stack is torn down
 * afterwards. Locally an already-running stack on the same ports is reused.
 *
 * Browsers live in PLAYWRIGHT_BROWSERS_PATH (C:\CricAi\cache\ms-playwright on
 * the Windows build machine; install once with `pnpm exec playwright install chromium`).
 */

import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

// Artifacts MUST live outside apps/web: `next dev` watches that tree, and a
// screenshot written there hot-reloads the page mid-test (a failure then
// cascades into the next test). .cricai-run/ is already git-ignored.
const RUN_DIR = join(__dirname, "..", "..", ".cricai-run", "e2e");

const API_PORT = 8100;
const WEB_PORT = 3100;
const HOST = "127.0.0.1"; // bind address for the API and the web server
// The browser uses "localhost": Next.js normalizes every loopback address in
// request.nextUrl to "localhost", so middleware redirects from 127.0.0.1 land on
// localhost and the session cookie would be split across two hosts.
const BROWSER_HOST = "localhost";

// Point the journeys at an already-running stack instead of booting one:
//   E2E_WEB_URL=http://127.0.0.1:3000 E2E_API_URL=http://127.0.0.1:8000 pnpm e2e
const EXTERNAL_WEB = process.env.E2E_WEB_URL;

export default defineConfig({
  testDir: "./e2e",
  outputDir: join(RUN_DIR, "results"),
  fullyParallel: false, // one shared in-memory database
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: join(RUN_DIR, "report") }]] : "list",
  use: {
    baseURL: EXTERNAL_WEB ?? `http://${BROWSER_HOST}:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "tablet",
      use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 }, hasTouch: true },
    },
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: EXTERNAL_WEB
    ? undefined
    : {
        command: `uv run scripts/dev_stack.py --web-mode prod --host ${HOST} --api-port ${API_PORT} --web-port ${WEB_PORT}`,
        cwd: "../..",
        url: `http://${HOST}:${WEB_PORT}/login`,
        reuseExistingServer: !process.env.CI,
        timeout: 600_000, // includes `next build` on a shared, busy machine
        stdout: "pipe",
        stderr: "pipe",
      },
});
