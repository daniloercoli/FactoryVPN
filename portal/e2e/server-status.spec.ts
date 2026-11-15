import { execFileSync } from "node:child_process";
import { completeEmail } from "./email";
import { test, expect, type Page } from "./fixtures";

async function login(page: Page, username: string) {
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
const seed = (...args: string[]) =>
  execFileSync("../.venv/bin/python", ["-m", "portal_metrics_seed", ...args], {
    stdio: "pipe",
  });

test("server operational states expire during a stalled refresh even with a backward clock", async ({
  page,
}) => {
  await page.clock.install();
  await login(page, "root");
  let hold = false;
  const pending: import("@playwright/test").Route[] = [];
  await page.route("**/api/v1/admin/server", async (route) => {
    if (hold) {
      pending.push(route);
      return;
    }
    await route.continue();
  });
  await page.goto("/app/server");
  const api = page.getByRole("row").filter({ hasText: "API FactoryVPN" });
  await expect(api).toContainText("Operativo");
  hold = true;
  await page.clock.setSystemTime(new Date("2020-01-01T00:00:00Z"));
  await page.clock.runFor(31000);
  await expect(api).toContainText("Non verificabile");
  const certificates = page.getByRole("region", {
    name: "Certificati configurati",
  });
  await expect(
    certificates.getByText("Da aggiornare", { exact: true }),
  ).toHaveCount(3);
  const backup = page.getByRole("region", { name: "Riepilogo backup" });
  await expect(backup).toContainText("Non verificabile");
  await expect(backup).toContainText("Osservazione scaduta");
  expect(pending.length).toBeGreaterThan(0);
  for (const route of pending) await route.abort();
});

test("global server status, real local SMTP check and explicit email, responsive charts", async ({
  page,
  request,
}) => {
  seed();
  const response = await request.post("/api/v1/internal/telemetry/server", {
    headers: { "X-Worker-Key": process.env.WORKER_API_KEY! },
    data: {
      observed_at: new Date().toISOString(),
      metrics: { cpu_percent: 25 },
      services: { worker: "ok", openvpn: "ok" },
      runtime: {
        version: "2.0.0",
        python: "3.13.2",
        openvpn: "2.6.15",
        build: "browser-test",
      },
    },
  });
  expect(response.ok()).toBeTruthy();
  await login(page, "root");
  await page.getByRole("link", { name: "Stato server", exact: true }).click();
  const components = page.getByRole("region", { name: "Componenti centrali" });
  await expect(components).toContainText("2.6.15");
  await expect(
    components.getByRole("row").filter({ hasText: "PostgreSQL" }),
  ).toContainText("Operativo");
  const certificates = page.getByRole("region", {
    name: "Certificati configurati",
  });
  await expect(
    certificates.getByRole("row").filter({ hasText: "Portale HTTPS" }),
  ).toContainText("In scadenza entro 30 giorni");
  await expect(
    certificates.getByRole("row").filter({ hasText: "Server OpenVPN" }),
  ).toContainText("Validità temporale regolare");
  await certificates.getByText("Dettagli del certificato").first().click();
  const pki = page.getByRole("region", { name: "Autorità e liste di revoca" });
  await expect(pki).toContainText("Emissione e CRL disponibili");
  await expect(pki).toContainText("Sub-CA emittente");
  await expect(pki).toContainText("Ultima data per emettere client");
  await expect(certificates.locator("code").first()).toHaveText(
    /^[a-f0-9]{64}$/,
  );
  const backup = page.getByRole("region", { name: "Riepilogo backup" });
  await expect(backup).toContainText("Copie a catalogo");
  await expect(backup).toContainText("Mai eseguita");
  await expect(
    backup.getByRole("link", { name: "Gestisci backup" }),
  ).toHaveAttribute("href", "/app/backups");
  const smtp = page.getByRole("region", { name: "Verifica SMTP" });
  await smtp.getByRole("button", { name: "Verifica connessione" }).click();
  await expect(smtp).toContainText("Connessione SMTP riuscita");
  await expect(
    smtp.getByRole("button", { name: "Invia email di prova" }),
  ).toBeDisabled();
  seed("--reset-smtp");
  await page
    .getByRole("button", { name: "Aggiorna stato", exact: true })
    .click();
  await smtp.getByRole("button", { name: "Invia email di prova" }).click();
  const confirmation = page.getByRole("dialog");
  await expect(confirmation).toContainText("root@example.test");
  await confirmation
    .getByRole("button", { name: "Invia email", exact: true })
    .click();
  await expect(smtp).toContainText("Email accettata dal server SMTP");
  const deliveries = await request.get(
    process.env.FACTORYVPN_MAIL_URL + "/test-deliveries/root@example.test",
  );
  expect((await deliveries.json()).count).toBe(1);
  const history = page.getByRole("region", { name: "Storico delle metriche" });
  await expect(history.getByRole("img")).toHaveCount(3);
  // Two line segments preserve the known gap in the stored series.
  expect(
    await history
      .locator(".metric-chart")
      .first()
      .locator(".chart-mean polyline")
      .count(),
  ).toBe(2);
  await history
    .getByRole("combobox", { name: "Periodo", exact: true })
    .selectOption("30d");
  await expect(history).toContainText("Intervalli di 60 minuti");
  await history
    .getByRole("combobox", { name: "Periodo", exact: true })
    .selectOption("24h");
  await expect(history).toContainText("Intervalli di 5 minuti");
  const slider = history.getByRole("slider");
  await slider.focus();
  await slider.press("Home");
  await expect(history.getByRole("article").first()).toContainText(
    "Nessun dato",
  );
  await slider.press("End");
  await history.getByText("Valori in tabella", { exact: true }).click();
  await expect(history.getByRole("row")).toHaveCount(290);
  await history.getByText("Valori in tabella", { exact: true }).click();
  await page.screenshot({
    path: "test-results/server-status-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/server-status-mobile.png",
    fullPage: true,
  });
});

test("server readiness distinguishes empty, failed and unavailable observations", async ({
  page,
}) => {
  await login(page, "root");
  let scenario = "empty";
  await page.route("**/api/v1/admin/server", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.backup.state =
      scenario === "unavailable"
        ? "unavailable"
        : scenario === "disabled"
          ? "not_configured"
          : "available";
    body.backup.coordinator_state = "offline";
    body.backup.copies = 0;
    body.backup.latest_backup = null;
    body.backup.last_test =
      scenario === "failed"
        ? { state: "failed", backup_id: null, finished_at: body.observed_at }
        : null;
    body.certificates[0].state = "expired";
    body.certificates[1].state = "unreadable";
    body.certificates[2].state = "missing";
    await route.fulfill({ response, json: body });
  });
  await page.goto("/app/server");
  const backup = page.getByRole("region", { name: "Riepilogo backup" });
  await expect(backup).toContainText("Nessuna copia disponibile");
  await expect(backup).toContainText("Nessun segnale recente");
  const certs = page.getByRole("region", { name: "Certificati configurati" });
  await expect(certs).toContainText("Scaduto");
  await expect(certs).toContainText("Certificato non leggibile");
  await expect(certs).toContainText("Certificato assente");
  scenario = "failed";
  await page
    .getByRole("button", { name: "Aggiorna stato", exact: true })
    .click();
  await expect(backup).toContainText("Non riuscita");
  scenario = "unavailable";
  await page
    .getByRole("button", { name: "Aggiorna stato", exact: true })
    .click();
  await expect(backup).toContainText("Il catalogo backup non è leggibile");
  await expect(
    page.getByRole("region", { name: "Componenti centrali" }),
  ).toContainText("PostgreSQL");
  scenario = "disabled";
  await page
    .getByRole("button", { name: "Aggiorna stato", exact: true })
    .click();
  await expect(backup).toContainText("Il servizio di backup non è configurato");
});

test("delegates see gateway charts including SMS gaps but cannot access server status or history", async ({
  page,
}) => {
  seed();
  await login(page, "delegate");
  await expect(
    page.getByRole("link", { name: "Stato server", exact: true }),
  ).toHaveCount(0);
  await page.goto("/app/server");
  await expect(
    page.getByText("Questa pagina è riservata agli amministratori globali."),
  ).toBeVisible();
  expect((await page.request.get("/api/v1/admin/server")).status()).toBe(403);
  expect(
    (await page.request.get("/api/v1/telemetry/server/history")).status(),
  ).toBe(403);
  await page.goto("/app/gateways/00000000-0000-4000-8000-000000000023");
  const history = page.getByRole("region", { name: "Storico delle metriche" });
  await expect(history).toContainText(
    "I gateway SMS inviano misure quando sono attivi",
  );
  await expect(history.getByRole("img")).toHaveCount(3);
  const denied = await page.request.get(
    "/api/v1/telemetry/gateways/00000000-0000-4000-8000-000000000021/history",
  );
  expect(denied.status()).toBe(404);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
