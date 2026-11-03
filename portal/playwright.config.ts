import { defineConfig, devices } from "@playwright/test";
if (!process.env.FACTORYVPN_PORTAL_URL)
  throw new Error("Use python -m scripts.test_with_postgres --portal");
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  expect: { timeout: 7000 },
  reporter: "list",
  outputDir: "test-results/browser",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: process.env.FACTORYVPN_PORTAL_URL,
    ignoreHTTPSErrors: true,
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
