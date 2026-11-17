import { appName } from "./branding";
import { completeEmail, completeApi } from "./email";
import { test, expect, type Page } from "./fixtures";
const origin = process.env.FACTORYVPN_PORTAL_URL!;
const password = "factoryvpn-browser-tests";
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const keys = JSON.parse(process.env.FACTORYVPN_TEST_GATEWAY_KEYS!);
async function login(page: Page, username: string) {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
}
test("telemetry shows scoped metrics, stale SMS data and restricts the server to global admins", async ({
  page,
  request,
  browser,
}) => {
  const metrics = {
    cpu_percent: 25,
    memory_total_bytes: 1073741824,
    memory_available_bytes: 536870912,
    disk_total_bytes: 10737418240,
    disk_available_bytes: 1073741824,
    uptime_seconds: 120,
  };
  for (const n of [20, 21, 23]) {
    const response = await request.post("/api/v1/gateways/telemetry", {
      headers: { "X-Gateway-Key": keys[id(n)] },
      data: {
        observed_at: new Date(
          Date.now() - (n === 23 ? 120000 : 0),
        ).toISOString(),
        metrics,
        services: {
          agent: "ok",
          control: "ok",
          openvpn: "idle",
          network: "idle",
        },
      },
    });
    expect(response.status()).toBe(200);
  }
  const report = await request.post("/api/v1/internal/telemetry/server", {
    headers: { "X-Worker-Key": process.env.WORKER_API_KEY! },
    data: {
      observed_at: new Date().toISOString(),
      metrics,
      services: { worker: "ok", openvpn: "ok" },
    },
  });
  expect(report.status()).toBe(200);
  await login(page, "delegate");
  await page.getByRole("link", { name: "Telemetria", exact: true }).click();
  const north = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Pressa Nord", exact: true }),
  });
  await expect(
    north.getByRole("progressbar", { name: "CPU", exact: true }),
  ).toHaveAttribute("value", "25");
  await expect(north).toContainText("512 MiB disponibili su 1 GiB");
  await expect(page.getByText("Pressa Sud", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Telemetria server" }),
  ).toHaveCount(0);
  expect((await page.request.get("/api/v1/telemetry/server")).status()).toBe(
    403,
  );
  const sms = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Gateway SMS Nord", exact: true }),
  });
  await expect(sms).toContainText("Dati scaduti");
  await expect(sms.getByRole("progressbar")).toHaveCount(0);
  await page.screenshot({
    path: "test-results/telemetry-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/telemetry-mobile.png",
    fullPage: true,
  });
  const root = await browser.newContext({ baseURL: origin });
  const admin = await root.newPage();
  await login(admin, "root");
  await admin.goto("/app/telemetry");
  await expect(
    admin.getByRole("region", { name: "Telemetria server" }),
  ).toContainText(`Server ${appName}`);
  await expect(
    admin.getByRole("heading", { name: "Pressa Sud", exact: true }),
  ).toBeVisible();
  await root.close();
});

test("a technician cannot obtain telemetry through navigation or direct API reads", async ({
  page,
}) => {
  await login(page, "technician");
  await expect(
    page.getByRole("link", { name: "Telemetria", exact: true }),
  ).toHaveCount(0);
  await page.goto("/app/telemetry");
  await expect(
    page.getByRole("heading", { name: "Accesso non disponibile" }),
  ).toBeVisible();
  expect((await page.request.get("/api/v1/telemetry/gateways")).status()).toBe(
    403,
  );
});

test("telemetry expires locally while a refresh hangs, independent of the browser wall clock", async ({ page }) => {
  await page.clock.install();
  await login(page, "delegate");
  const pending: import("@playwright/test").Route[] = [];
  let blockRefresh = false;
  await page.route("**/api/v1/telemetry/gateways?*", async (route) => {
    if (blockRefresh) {
      pending.push(route);
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "application/json", "X-Total-Count": "1" },
      body: JSON.stringify([{
        gateway_id: id(20), display_name: "Campione in scadenza",
        freshness: "fresh", valid_for_seconds: 30,
        observed_at: new Date().toISOString(), received_at: new Date().toISOString(),
        metrics: { cpu_percent: 30 },
        services: { agent: "ok", control: "ok", openvpn: "ok", network: "ok" },
      }]),
    });
  });
  await page.goto("/app/telemetry");
  const card = page.getByRole("article").filter({ hasText: "Campione in scadenza" });
  await expect(card.getByRole("progressbar", { name: "CPU", exact: true })).toBeVisible();
  // Authentication may cancel/retry the initial read. Block only after a sample
  // is actually displayed, so the test exercises a suspended refresh, not startup.
  blockRefresh = true;
  await page.clock.setSystemTime(new Date("2020-01-01T00:00:00Z"));
  await page.clock.runFor(31000);
  await expect(card).toContainText("Dati scaduti");
  await expect(card.getByRole("progressbar")).toHaveCount(0);
  await expect(card.getByText("Operativo", { exact: true })).toHaveCount(0);
  await expect.poll(() => pending.length).toBeGreaterThan(0);
  await expect(card).toContainText("Dati scaduti");
  for (const route of pending) await route.abort();
});
