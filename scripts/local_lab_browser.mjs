// Real portal -> gateway -> OpenVPN -> gateway web smoke test against the local lab.
// Requires the portal's Playwright dependency and its Chromium installation.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve, sep } from "node:path";
import { parseArgs } from "node:util";

const root = fileURLToPath(new URL("../", import.meta.url));
const { values } = parseArgs({
  options: { simulator: { type: "boolean" }, directory: { type: "string" } },
});
const simulator = Boolean(values.simulator);
const labDir = values.directory
  ? pathToFileURL(resolve(values.directory) + sep)
  : new URL(
      simulator ? "../.factoryvpn-simulator/" : "../.factoryvpn-lab/",
      import.meta.url,
    );
const labSetup =
  "import sys; from pathlib import Path; from scripts.local_lab import Lab; " +
  "lab=Lab(Path(sys.argv[1])); ";
const state = JSON.parse(readFileSync(new URL("lab.json", labDir), "utf8"));
assert.equal(state.purpose, "factoryvpn-local-lab");
assert.equal(Boolean(state.simulator), simulator);
assert.match(state.origin, /^https:\/\/localhost:\d+$/);
const credentials = JSON.parse(
  readFileSync(new URL("credentials.json", labDir), "utf8"),
);
const require = createRequire(
  new URL("../portal/package.json", import.meta.url),
);
const { chromium, expect } = require("@playwright/test");
const python = fileURLToPath(new URL("../.venv/bin/python", import.meta.url));
const pythonRun = (code, ...args) =>
  execFileSync(
    python,
    ["-c", labSetup + code, fileURLToPath(labDir), ...args],
    {
      cwd: root,
      encoding: "utf8",
      timeout: 120_000,
    },
  ).trim();
const peer = (action, protocol = "tcp") =>
  pythonRun("print(lab.peer(sys.argv[2], 1, sys.argv[3]))", action, protocol);
const disconnect = () =>
  execFileSync(
    python,
    [
      "-m",
      simulator ? "scripts.gateway_simulator" : "scripts.local_lab",
      "disconnect",
      "--directory",
      fileURLToPath(labDir),
      "--site",
      "1",
    ],
    { cwd: root, encoding: "utf8", timeout: 120_000 },
  );

disconnect();
const browser = await chromium.launch({ headless: true });
try {
  // This exception applies only to the loopback lab with its private test CA.
  const page = await browser.newPage({
    baseURL: state.origin,
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 1050 },
  });
  page.setDefaultTimeout(60_000);
  await page.goto("/app/login");
  await page
    .getByLabel("Username", { exact: true })
    .fill(credentials.tech1.username);
  await page
    .getByLabel("Password", { exact: true })
    .fill(credentials.tech1.password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
  await page.goto("/app/vpn");
  await page
    .getByRole("button", {
      name: "Apri VPN Gateway laboratorio 1",
      exact: true,
    })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(page).toHaveURL(/\/app\/vpn\/[0-9a-f-]{36}$/);
  const sessionId = page.url().split("/").pop();
  pythonRun(
    "from uuid import UUID; lab.state['sites']['1']['session_id']=str(UUID(sys.argv[2])); lab.persist()",
    sessionId,
  );
  if (simulator) {
    assert.equal(JSON.parse(peer("sms-status")).pending, 1);
    assert.equal(peer("query"), "blocked");
    await page.screenshot({
      path: fileURLToPath(new URL("portal-awaiting-sms.png", labDir)),
      fullPage: true,
    });
    pythonRun(
      "import json; from contextlib import closing; " +
        "account=json.loads((lab.root/'credentials.json').read_text())['tech1']; " +
        "client=lab.login(account); " +
        "lab.receive_pending_sms(client, 1, sys.argv[2]); client.close()",
      sessionId,
    );
    console.log(
      "PASS: SMS remains queued and VPN access blocked until simulated serial delivery",
    );
  }
  await expect(
    page.getByText(/Il gateway è pronto. Apri manualmente/),
  ).toBeVisible({ timeout: 60_000 });
  peer("client-start");
  await expect(
    page.getByRole("heading", { name: "VPN attiva", exact: true }),
  ).toBeVisible({ timeout: 60_000 });
  for (const protocol of ["tcp", "https", "tcp-alt", "gateway-udp"])
    assert.equal(peer("query", protocol), "site1");
  assert.equal(peer("query", "icmp"), "reachable");
  assert.equal(peer("query", "lan-udp"), "blocked");
  await page.screenshot({
    path: fileURLToPath(new URL("portal-active.png", labDir)),
    fullPage: true,
  });
  console.log(
    "PASS: browser opens a real VPN; gateway HTTP/HTTPS/TCP8080/UDP9000 and ICMP pass; LAN stays blocked",
  );
  await page
    .getByRole("button", { name: "Chiudi sessione", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Conferma chiusura", exact: true })
    .click();
  await expect(
    page.getByText("Risorse rilasciate.", { exact: false }),
  ).toBeVisible({ timeout: 60_000 });
  for (const protocol of [
    "tcp",
    "https",
    "tcp-alt",
    "gateway-udp",
    "icmp",
    "lan-udp",
  ])
    assert.equal(peer("query", protocol), "blocked");
  console.log(
    "PASS: browser closes the VPN; gateway TCP/UDP/ICMP traffic is blocked after cleanup",
  );
  writeFileSync(
    new URL("browser-report.json", labDir),
    JSON.stringify(
      {
        timestamp: new Date().toISOString(),
        session_id: sessionId,
        checks: [
          "portal login",
          ...(simulator
            ? ["SMS queued without activation", "manual serial SMS delivery"]
            : []),
          "gateway ready",
          "real VPN ACTIVE",
          "HTTP/HTTPS/TCP8080/UDP9000 site1 and ICMP reachable; direct LAN denied",
          "portal close",
          "traffic blocked",
        ],
      },
      null,
      2,
    ) + "\n",
    { mode: 0o600 },
  );
} finally {
  try {
    disconnect();
  } finally {
    await browser.close();
  }
}
