import { appName } from "./branding";
import { randomUUID } from "node:crypto";
import { completeEmail } from "./email";
import { test, expect, type Page, type APIRequestContext } from "./fixtures";

const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const keys = JSON.parse(process.env.FACTORYVPN_TEST_GATEWAY_KEYS!);
async function login(page: Page, username = "delegate") {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page
    .getByLabel("Password", { exact: true })
    .fill("factoryvpn-browser-tests");
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
}
async function report(
  request: APIRequestContext,
  n: number,
  components: object,
) {
  const response = await request.post("/api/v1/gateways/system-info", {
    headers: { "X-Gateway-Key": keys[id(n)] },
    data: {
      report_id: randomUUID(),
      observed_at: new Date().toISOString(),
      components,
    },
  });
  expect(response.status()).toBe(200);
}
async function log(request: APIRequestContext) {
  const now = new Date().toISOString();
  const response = await request.post("/api/v1/gateways/technical-logs", {
    headers: { "X-Gateway-Key": keys[id(20)] },
    data: {
      events: [
        {
          event_id: randomUUID(),
          observed_at: now,
          first_observed_at: now,
          code: "api_unavailable",
          parameters: { http_status: 503 },
        },
      ],
    },
  });
  expect(response.status()).toBe(200);
}

test("Wi-Fi identity, component failures, filtered logs, CSV and mobile layout", async ({
  page,
  request,
}) => {
  await report(request, 20, {
    agent: {
      state: "reported",
      version: "2.0.0",
      build: "browser-test",
      origin: "build",
    },
    openvpn: { state: "reported", version: "2.6.14" },
    wifi: {
      state: "reported",
      interface: "wlan0",
      mac_address: "AA:BB:CC:DD:EE:FF",
    },
    modem: { state: "not_applicable" },
  });
  await report(request, 20, { openvpn: { state: "collection_failed" } });
  await log(request);
  await login(page);
  await page.goto(`/app/gateways/${id(20)}`);
  const components = page.getByRole("region", {
    name: "Componenti e identificativi",
  });
  await expect(components).toContainText("AA:BB:CC:DD:EE:FF");
  await expect(components).toContainText("Non applicabile");
  await expect(components).toContainText("Rilevamento non riuscito");
  await expect(components).toContainText("2.6.14");
  await expect(components.getByText("IMEI modem", { exact: true })).toHaveCount(
    0,
  );
  const logs = page
    .locator("details")
    .filter({ has: page.getByText("Log tecnici", { exact: true }) });
  await logs.getByLabel("Codice", { exact: true }).fill("api_unavailable");
  await logs.getByRole("button", { name: "Filtra log" }).click();
  await expect(
    logs
      .getByRole("row")
      .filter({ hasText: "Contatto API non riuscito" })
      .first(),
  ).toBeVisible();
  const download = page.waitForEvent("download");
  await logs.getByRole("button", { name: "Esporta CSV" }).click();
  expect((await download).suggestedFilename()).toBe(
    "factoryvpn-gateway-technical-logs.csv",
  );
  await logs.getByRole("button", { name: "Sospendi aggiornamento" }).click();
  await expect(
    logs.getByRole("button", { name: "Riprendi aggiornamento" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/components-logs-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/components-logs-mobile.png",
    fullPage: true,
  });
  await logs.getByLabel("Codice", { exact: true }).fill("unknown_code");
  await logs.getByRole("button", { name: "Filtra log" }).click();
  await expect(logs).toContainText("Nessun evento nell’intervallo selezionato");
});

test("SIM shows IMEI; technicians without logs.read see components only", async ({
  page,
  request,
}) => {
  await report(request, 23, {
    modem: {
      state: "reported",
      imei: "123456789012345",
      model: "Test modem",
      version: "1.2",
    },
    wifi: { state: "unsupported" },
  });
  await login(page, "technician");
  await page.goto(`/app/gateways/${id(23)}`);
  await expect(
    page.getByRole("region", { name: "Componenti e identificativi" }),
  ).toContainText("123456789012345");
  await expect(page.getByText("Log tecnici", { exact: true })).toHaveCount(0);
  expect(
    (
      await page.request.get(`/api/v1/gateways/${id(23)}/technical-logs`)
    ).status(),
  ).toBe(403);
});

test("late log response cannot reappear after logout", async ({
  page,
  request,
}) => {
  await log(request);
  await login(page);
  let release!: () => void;
  let received!: () => void;
  const waiting = new Promise<void>((resolve) => {
    received = resolve;
  });
  const resume = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    `**/api/v1/gateways/${id(20)}/technical-logs?*`,
    async (route) => {
      const response = await route.fetch();
      received();
      await resume;
      await route.fulfill({ response }).catch(() => {});
    },
  );
  await page.goto(`/app/gateways/${id(20)}`);
  await waiting;
  await page
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: `Accedi a ${appName}` }),
  ).toBeVisible();
  release();
  await expect(page.getByText("Log tecnici", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("Contatto API non riuscito", { exact: true }),
  ).toHaveCount(0);
});
