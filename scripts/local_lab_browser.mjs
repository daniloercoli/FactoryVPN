// Real portal -> gateway -> OpenVPN -> gateway web smoke test against the local lab.
// Requires the portal's Playwright dependency and Chromium, or --browser-channel chrome.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { posix, resolve } from "node:path";
import { parseArgs } from "node:util";

const root = fileURLToPath(new URL("../", import.meta.url));
const { values } = parseArgs({
  options: {
    simulator: { type: "boolean" },
    directory: { type: "string" },
    "wsl-distribution": { type: "string" },
    "wsl-project-directory": { type: "string" },
    "browser-channel": { type: "string" },
  },
});
const simulator = Boolean(values.simulator);
const distribution = values["wsl-distribution"];
assert(
  !distribution || process.platform === "win32",
  "WSL control requires Windows Node",
);
assert(
  distribution || !values["wsl-project-directory"],
  "--wsl-project-directory requires --wsl-distribution",
);
const linuxRoot = values["wsl-project-directory"] ?? "/root/factoryvpn";
if (distribution)
  assert(posix.isAbsolute(linuxRoot), "Use an absolute Linux project path");
const defaultLab = simulator ? ".factoryvpn-simulator" : ".factoryvpn-lab";
// Host files/dependencies and Linux lab paths deliberately use separate roots.
const labDir = distribution
  ? posix.resolve(linuxRoot, values.directory ?? defaultLab)
  : resolve(values.directory ?? resolve(root, defaultLab));
const labFile = (name) =>
  distribution ? posix.join(labDir, name) : resolve(labDir, name);
const python = fileURLToPath(new URL("../.venv/bin/python", import.meta.url));
const runPython = (args, options = {}) =>
  execFileSync(
    distribution ? "wsl.exe" : python,
    distribution
      ? [
          "-d",
          distribution,
          "-u",
          "root",
          "--cd",
          linuxRoot,
          "--",
          ".venv/bin/python",
          ...args,
        ]
      : args,
    { cwd: root, encoding: "utf8", timeout: 120_000, ...options },
  );
const readLabText = (name) =>
  distribution
    ? runPython([
        "-c",
        "import sys; from pathlib import Path; sys.stdout.write(Path(sys.argv[1]).read_text(encoding='utf-8'))",
        labFile(name),
      ])
    : readFileSync(labFile(name), "utf8");
const writeLabFile = (name, data) => {
  if (distribution) {
    // Transfer private artifacts through stdin, without exposing content in arguments/logs.
    runPython(
      [
        "-c",
        "import sys; from pathlib import Path; from factoryvpn_common.certificates import write_private; " +
          "target=Path(sys.argv[1]); write_private(target, sys.stdin.buffer.read(), replace=target.exists())",
        labFile(name),
      ],
      { input: data },
    );
  } else {
    writeFileSync(labFile(name), data, { mode: 0o600 });
  }
};
const screenshot = async (page, name) =>
  writeLabFile(name, await page.screenshot({ fullPage: true }));
const labSetup =
  "import sys; from pathlib import Path; from scripts.local_lab import Lab; " +
  "lab=Lab(Path(sys.argv[1])); ";
const state = JSON.parse(readLabText("lab.json"));
assert.equal(state.purpose, "factoryvpn-local-lab");
assert.equal(Boolean(state.simulator), simulator);
assert.match(state.origin, /^https:\/\/localhost:\d+$/);
const credentials = JSON.parse(readLabText("credentials.json"));
const require = createRequire(
  new URL("../portal/package.json", import.meta.url),
);
const { chromium, expect } = require("@playwright/test");
const pythonRun = (code, ...args) =>
  runPython(["-c", labSetup + code, labDir, ...args]).trim();
const peer = (action, protocol = "tcp") =>
  pythonRun("print(lab.peer(sys.argv[2], 1, sys.argv[3]))", action, protocol);
const disconnect = () =>
  runPython([
    "-m",
    simulator ? "scripts.gateway_simulator" : "scripts.local_lab",
    "disconnect",
    "--directory",
    labDir,
    "--site",
    "1",
  ]);

disconnect();
const browser = await chromium.launch({
  headless: true,
  ...(values["browser-channel"] ? { channel: values["browser-channel"] } : {}),
});
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
    await screenshot(page, "portal-awaiting-sms.png");
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
  await screenshot(page, "portal-active.png");
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
  writeLabFile(
    "browser-report.json",
    JSON.stringify(
      {
        timestamp: new Date().toISOString(),
        origin: state.origin,
        browser_channel: values["browser-channel"] ?? "chromium",
        wsl_distribution: distribution ?? null,
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
  );
} finally {
  try {
    disconnect();
  } finally {
    await browser.close();
  }
}
