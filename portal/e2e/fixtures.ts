import { test as base, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
// Restore only machine fixtures between tests. Group transfer now revokes their
// keys; test files must not depend on certificates left by previous scenarios.
export const test = base.extend<{ machineFixtures: void }>({
  machineFixtures: [async ({}, use) => {
    execFileSync("../.venv/bin/python", ["-m", "portal_seed", "--credentials-only"], { stdio: "pipe" });
    await use();
  }, { auto: true }],
});
export { expect };

export type { Page, APIRequestContext, BrowserContext } from "@playwright/test";
