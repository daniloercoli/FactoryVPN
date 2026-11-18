import { test as base, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
// Restore machine credentials, email limits and the unprocessed backup queue
// between tests so scenarios do not inherit earlier revocations or queued jobs.
export const test = base.extend<{ machineFixtures: void }>({
  machineFixtures: [async ({}, use) => {
    execFileSync("../.venv/bin/python", ["-m", "portal_seed", "--credentials-only"], { stdio: "pipe" });
    await use();
  }, { auto: true }],
});
export { expect };

export type { Page, APIRequestContext, BrowserContext } from "@playwright/test";
