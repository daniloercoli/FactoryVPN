import { completeEmail, completeApi } from "./email";
import {
  test,
  expect,
  type Page,
  type BrowserContext,
  type APIRequestContext,
} from "./fixtures";

const origin = process.env.FACTORYVPN_PORTAL_URL!;
const password = "factoryvpn-browser-tests";
const identifier = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const workerHeaders = { "X-Worker-Key": process.env.WORKER_API_KEY! };
const keys = JSON.parse(process.env.FACTORYVPN_TEST_GATEWAY_KEYS!);

async function login(page: Page, user = "technician") {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(user);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, user);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
}
async function call(
  context: BrowserContext,
  path: string,
  method = "GET",
  data?: unknown,
) {
  const meta = await (await context.request.get("/api/v1/web/session")).json();
  return context.request.fetch("/api/v1" + path, {
    method,
    data,
    headers: {
      Origin: origin,
      "X-FactoryVPN-Portal": "1",
      "X-CSRF-Token": meta.csrf_token,
    },
  });
}
async function open(page: Page, name = "Pressa Nord") {
  await page.goto("/app/vpn");
  await page
    .getByRole("button", { name: `Apri VPN ${name}`, exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(page).toHaveURL(/\/app\/vpn\/[0-9a-f-]{36}$/);
  return page.url().split("/").pop()!;
}
async function cleaned(request: APIRequestContext, id: string) {
  const response = await request.post(
    `/api/v1/internal/sessions/${id}/cleaned`,
    { headers: workerHeaders },
  );
  expect(response.status()).toBe(200);
}
test.afterEach(async ({ request }) => {
  // Only synthetic data from the disposable runner. No worker/network is started.
  const auth = await (
    await completeApi(request, await request.post("/api/v1/auth/login", {
      data: { username: "root", password },
    }))
  ).json();
  const headers = { Authorization: "Bearer " + auth.access_token };
  const rows = await (
    await request.get("/api/v1/admin/sessions?reserved=true", { headers })
  ).json();
  for (const row of rows) {
    await request.delete(`/api/v1/sessions/${row.id}`, { headers });
    await cleaned(request, row.id);
  }
});

test("VPN opening recovers a lost response and follows gateway, active, close and cleanup", async ({
  page,
  context,
  request,
}) => {
  await login(page);
  const requestKeys: string[] = [];
  let lose = true;
  await page.route("**/api/v1/sessions/", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    requestKeys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    if (lose) {
      lose = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page.goto("/app/vpn");
  await expect(page.getByText("Pressa Sud", { exact: true })).toHaveCount(0);
  await page
    .getByRole("button", { name: "Apri VPN Pressa Nord", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Impossibile raggiungere",
  );
  await dialog
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(page).toHaveURL(/\/app\/vpn\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  expect(requestKeys).toHaveLength(2);
  expect(requestKeys[0]).toBe(requestKeys[1]);
  const own = await (await call(context, "/sessions/?reserved=true")).json();
  expect(own).toHaveLength(1);
  await expect(
    page.getByRole("heading", { name: "Attesa del gateway Wi-Fi" }),
  ).toBeVisible();
  await page.reload();
  const detail = await (await call(context, `/sessions/${id}`)).json();
  const heartbeat = await request.post("/api/v1/gateways/heartbeat", {
    headers: { "X-Gateway-Key": keys[identifier(20)] },
    data: { ready_session_id: id, vpn_ip: detail.gateway_vpn_ip },
  });
  expect(heartbeat.status()).toBe(200);
  await expect(
    page.getByText(/Il gateway è pronto. Apri manualmente/),
  ).toBeVisible();
  expect(
    (
      await request.post(`/api/v1/internal/sessions/${id}/ready`, {
        headers: workerHeaders,
      })
    ).status(),
  ).toBe(200);
  await expect(
    page.getByRole("heading", { name: "VPN attiva", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/vpn-active-desktop.png",
    fullPage: true,
  });
  await page.route(`**/api/v1/sessions/${id}`, (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: '{"detail":"unavailable"}',
    }),
  );
  await page.getByRole("button", { name: "Aggiorna stato" }).click();
  await expect(
    page.getByRole("heading", { name: "Stato VPN non disponibile" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "VPN attiva", exact: true }),
  ).toHaveCount(0);
  await page.unroute(`**/api/v1/sessions/${id}`);
  await page.reload();
  await page
    .getByRole("button", { name: "Chiudi sessione", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Conferma chiusura", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Rilascio delle risorse", level: 2 }),
  ).toBeVisible();
  await cleaned(request, id);
  await expect(
    page.getByText("Risorse rilasciate.", { exact: false }),
  ).toBeVisible();
});

test("VPN resource boundaries, CSRF and delegated supervision remain enforced", async ({
  page,
  context,
  browser,
}) => {
  await login(page);
  const id = await open(page);
  const bad = await context.request.delete(`/api/v1/sessions/${id}`, {
    headers: { Origin: origin },
  });
  expect(bad.status()).toBe(403);
  await page.goto("/app/vpn-supervision");
  await expect(
    page.getByRole("heading", { name: "Accesso non disponibile" }),
  ).toBeVisible();
  const other = await browser.newContext({ baseURL: origin });
  const second = await other.newPage();
  await login(second, "southern");
  await second.goto(`/app/vpn/${id}`);
  await expect(second.getByRole("alert")).toContainText(
    "Risorsa non disponibile",
  );
  expect((await call(other, `/sessions/${id}`, "DELETE")).status()).toBe(404);
  expect((await call(other, "/web/logout", "POST")).status()).toBe(200);
  await login(second, "delegate");
  await second.goto("/app/vpn-supervision");
  await second
    .getByRole("link", { name: `Dettaglio sessione ${id}`, exact: true })
    .click();
  await second
    .getByRole("button", { name: "Chiudi sessione", exact: true })
    .click();
  await expect(second.getByRole("dialog")).toContainText("altro tecnico");
  await second.getByRole("button", { name: "Conferma chiusura" }).click();
  await expect(
    second.getByRole("heading", { name: "Rilascio delle risorse", level: 2 }),
  ).toBeVisible();
  await other.close();
});

test("client supervision searches identities, expires evidence and closes with cleanup", async ({ page, browser, request }) => {
  test.setTimeout(60000);
  await login(page);
  const id = await open(page);
  const other = await browser.newContext({ baseURL: origin });
  const admin = await other.newPage();
  await login(admin, "delegate");
  await admin.getByRole("link", { name: "Supervisiona i client VPN" }).click();
  const row = admin.getByRole("row").filter({ hasText: id });
  await expect(row.getByRole("link", { name: "Tecnico Nord", exact: true })).toBeVisible();
  await expect(row.getByText("Non verificabile", { exact: true })).toHaveCount(3);
  await admin.getByLabel("Cerca client", { exact: true }).fill("southern");
  await expect(admin.getByText("Nessuna sessione con questi filtri")).toBeVisible();
  await admin.getByLabel("Cerca client", { exact: true }).fill("Tecnico Nord");
  await expect(row).toBeVisible();
  expect((await request.post(`/api/v1/internal/sessions/${id}/diagnostics`, {
    headers: workerHeaders,
    data: { observed_at: new Date().toISOString(), gateway_connected: true, technician_connected: true, network: "waiting", code: "none" },
  })).status()).toBe(200);
  await admin.getByRole("button", { name: "Aggiorna client", exact: true }).click();
  await expect(row.getByText("Connesso", { exact: true })).toHaveCount(2);
  await admin.screenshot({ path: "test-results/clients-desktop.png", fullPage: true });
  await admin.clock.install();
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await admin.route("**/api/v1/admin/sessions?**", async (route) => {
    const response = await route.fetch();
    await delayed;
    await route.fulfill({ response });
  });
  await admin.clock.fastForward(31000);
  await expect(row.getByText("Non verificabile", { exact: true })).toHaveCount(3);
  release();
  await admin.unroute("**/api/v1/admin/sessions?**");
  await admin.route("**/api/v1/admin/sessions?**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"unavailable"}' }));
  await admin.getByRole("button", { name: "Aggiorna client", exact: true }).click();
  await expect(admin.getByRole("alert")).toBeVisible();
  await expect(row).toHaveCount(0);
  await admin.unroute("**/api/v1/admin/sessions?**");
  await admin.getByRole("button", { name: "Aggiorna client", exact: true }).click();
  await expect(row).toBeVisible();
  await admin.setViewportSize({ width: 390, height: 844 });
  expect(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await admin.screenshot({ path: "test-results/clients-mobile.png", fullPage: true });
  await row.getByRole("button", { name: `Chiudi sessione ${id}`, exact: true }).click();
  await expect(admin.getByRole("dialog")).toContainText("interrompe l’assistenza");
  await admin.getByRole("button", { name: "Conferma chiusura", exact: true }).click();
  await expect(row).toContainText("Rilascio delle risorse");
  await expect(row.getByRole("button", { name: /Chiudi sessione/ })).toHaveCount(0);
  await admin.getByLabel("Mostra", { exact: true }).selectOption("cleanup");
  await expect(row).toBeVisible();
  await cleaned(request, id);
  await admin.getByRole("button", { name: "Aggiorna client", exact: true }).click();
  await expect(row).toHaveCount(0);
  await admin.getByLabel("Mostra", { exact: true }).selectOption("all");
  await expect(row).toContainText("Sessione terminata");
  await other.close();
});

test("client evidence expires during a delayed response despite a different server clock", async ({ page, browser, request }) => {
  await login(page);
  const id = await open(page);
  expect((await request.post(`/api/v1/internal/sessions/${id}/diagnostics`, {
    headers: workerHeaders,
    data: { observed_at: new Date().toISOString(), gateway_connected: true, technician_connected: true, network: "waiting", code: "none" },
  })).status()).toBe(200);
  const other = await browser.newContext({ baseURL: origin });
  const admin = await other.newPage();
  await login(admin, "delegate");
  await admin.clock.install();
  let delayed = false;
  let holding!: () => void;
  const held = new Promise<void>((resolve) => { holding = resolve; });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await admin.route("**/api/v1/admin/sessions?**", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    // Emulate an API clock one hour behind the browser. The evidence lifetime
    // is relative to generation, not to the browser's calendar.
    for (const row of data) {
      row.diagnostics.generated_at = new Date(Date.parse(row.diagnostics.generated_at) - 3600000).toISOString();
      if (row.observation_valid_until) row.observation_valid_until = new Date(Date.parse(row.observation_valid_until) - 3600000).toISOString();
    }
    if (delayed) { holding(); await gate; }
    await route.fulfill({ response, json: data });
  });
  await admin.goto("/app/clients");
  const row = admin.getByRole("row").filter({ hasText: id });
  await expect(row.getByText("Connesso", { exact: true })).toHaveCount(2);
  delayed = true;
  await admin.getByRole("button", { name: "Aggiorna client", exact: true }).click();
  await held;
  await admin.clock.fastForward(31000);
  await expect(row.getByText("Non verificabile", { exact: true })).toHaveCount(3);
  release();
  await expect(admin.getByRole("button", { name: "Aggiorna client", exact: true })).toBeEnabled();
  await expect(row.getByText("Non verificabile", { exact: true })).toHaveCount(3);
  await other.close();
});

test("revoking supervision clears its open confirmation and rejects administrative close", async ({ page, browser, request }) => {
  await login(page);
  const id = await open(page);
  const other = await browser.newContext({ baseURL: origin });
  const admin = await other.newPage();
  const root = await (await completeApi(request, await request.post("/api/v1/auth/login", {
    data: { username: "root", password },
  }))).json();
  const headers = { Authorization: "Bearer " + root.access_token };
  const userPath = `/api/v1/admin/users/${identifier(11)}`;
  const original = await (await request.get(userPath, { headers })).json();
  try {
    await login(admin, "delegate");
    await admin.clock.install();
    await admin.goto("/app/clients");
    await admin.getByRole("button", { name: `Chiudi sessione ${id}`, exact: true }).click();
    await expect(admin.getByRole("dialog")).toBeVisible();
    const metadata = await (await other.request.get("/api/v1/web/session")).json();
    expect((await request.put(userPath, {
      headers, data: { permissions: original.permissions.filter((p: string) => p !== "sessions.manage") },
    })).status()).toBe(200);
    expect((await other.request.delete(`/api/v1/admin/sessions/${id}`, {
      headers: { Origin: origin, "X-FactoryVPN-Portal": "1", "X-CSRF-Token": metadata.csrf_token },
    })).status()).toBe(401);
    expect((await (await request.get(`/api/v1/sessions/${id}`, { headers })).json()).status).toBe("REQUESTED");
    await admin.clock.fastForward(31000);
    await expect(admin.getByRole("heading", { name: "Accedi a FactoryVPN" })).toBeVisible();
    await expect(admin.getByRole("dialog")).toHaveCount(0);
    await expect(admin.getByText(id, { exact: true })).toHaveCount(0);
  } finally {
    expect((await request.put(userPath, { headers, data: { permissions: original.permissions } })).status()).toBe(200);
    await other.close();
  }
});

test("gateway administration filters availability and never claims availability is online", async ({ page, request }) => {
  expect((await request.post("/api/v1/gateways/heartbeat", {
    headers: { "X-Gateway-Key": keys[identifier(20)] }, data: {},
  })).status()).toBe(200);
  await login(page, "delegate");
  await page.goto("/app/gateways");
  await page.getByLabel("Cerca gateway", { exact: true }).fill("Pressa Nord");
  const row = page.getByRole("row").filter({ hasText: "Pressa Nord" });
  await expect(row).toContainText("Disponibile");
  await expect(row).toContainText("INACTIVE");
  await page.getByLabel("Disponibilità", { exact: true }).selectOption("busy");
  await expect(row).toHaveCount(0);
  await page.getByLabel("Disponibilità", { exact: true }).selectOption("");
  await page.getByLabel("Attivazione", { exact: true }).selectOption("sms");
  await expect(row).toHaveCount(0);
  await page.getByLabel("Cerca gateway", { exact: true }).fill("");
  await expect(page.getByRole("row").filter({ hasText: "Gateway SMS Nord" })).toBeVisible();
  await page.getByLabel("Attivazione", { exact: true }).selectOption("");
  await page.getByLabel("Cerca gateway", { exact: true }).fill("Pressa Sud");
  await expect(page.getByText("Nessun gateway nel tuo ambito")).toBeVisible();
});

test("SMS session reports receipt separately and prevents rapid resends on mobile", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const id = await open(page, "Gateway SMS Nord");
  await expect(
    page.getByRole("heading", { name: "Attesa ricezione SMS" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Reinvia SMS tra/ }),
  ).toBeDisabled();
  const rejected = await call(context, `/sessions/${id}/wake`, "POST");
  expect(rejected.status()).toBe(429);
  expect(Number(rejected.headers()["retry-after"])).toBeGreaterThan(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/vpn-sms-mobile.png",
    fullPage: true,
  });
});

for (const mode of ["local", "sso"]) {
  test(`${mode} portal logout closes its VPN and preserves another access`, async ({
    page,
    context,
    browser,
  }) => {
    if (mode === "sso") {
      await page.goto("/app/login");
      await page.getByRole("button", { name: "Provider di test" }).click();
      await page
        .getByRole("button", { name: "Continua come utente SSO" })
        .click();
      await expect(
        page.getByRole("heading", { name: "Accessi sotto controllo" }),
      ).toBeVisible();
    } else await login(page);
    const id = await open(page);
    const other = await browser.newContext({ baseURL: origin });
    const second = await other.newPage();
    await login(second, "root");
    await page
      .getByRole("button", { name: "Esci dal portale", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toContainText(
      "VPN aperte da questo accesso",
    );
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Esci dal portale", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Accedi a FactoryVPN" }),
    ).toBeVisible();
    const detail = await (await call(other, `/sessions/${id}`)).json();
    expect(detail.status).toBe("CLOSED");
    expect(detail.cleaned_at).toBeNull();
    expect((await context.request.get("/api/v1/web/session")).status()).toBe(
      401,
    );
    expect((await other.request.get("/api/v1/web/session")).status()).toBe(200);
    await other.close();
  });
}

test("gateway names are editable with revisions and searchable in VPN selection", async ({
  page,
}) => {
  await login(page, "delegate");
  await page.goto("/app/gateways");
  const row = page.getByRole("row").filter({ hasText: "02:00:00:00:00:01" });
  await row.getByRole("button", { name: "Modifica nome" }).click();
  await page.getByLabel("Nome descrittivo").fill("Pressa test <Nord>");
  await page.getByRole("button", { name: "Salva nome" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.goto("/app/vpn");
  await page.getByLabel("Cerca gateway").fill("Pressa test");
  await expect(
    page.getByText("Pressa test <Nord>", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Apri VPN Gateway SMS Nord" }),
  ).toHaveCount(0);
  await page.goto("/app/gateways");
  await row.getByRole("button", { name: "Modifica nome" }).click();
  await page.getByLabel("Nome descrittivo").fill("Pressa Nord");
  await page.getByRole("button", { name: "Salva nome" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("an external account change removes VPN details and pending confirmations", async ({
  page,
  context,
}) => {
  await page.clock.install();
  await login(page);
  const id = await open(page);
  await page
    .getByRole("button", { name: "Chiudi sessione", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  // Replace the cookie as another tab would, without a broadcast to this SPA.
  const changed = await context.request.post("/api/v1/web/login", {
    data: { username: "southern", password },
    headers: { Origin: origin, "X-FactoryVPN-Portal": "1" },
  });
  expect(changed.status()).toBe(200);
  const refreshed = page.waitForResponse("**/api/v1/web/session");
  await page.clock.fastForward(31000);
  await refreshed;
  await expect(page.locator(".sidebar-profile")).toContainText("southern");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText(id, { exact: true })).toHaveCount(0);
  await expect(page.getByText("10.100.0.2", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.getByRole("alert")).toContainText(
    "Risorsa non disponibile",
  );
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Le mie VPN", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Apri VPN Pressa Sud", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Pressa Nord", { exact: true })).toHaveCount(0);
});

test("logout in another tab closes its VPN and clears the first tab, preserving another browser", async ({
  page,
  context,
  browser,
}) => {
  await login(page);
  const id = await open(page);
  const other = await browser.newContext({ baseURL: origin });
  const independent = await other.newPage();
  await login(independent);
  const second = await context.newPage();
  await second.goto("/app/vpn");
  await expect(
    second.getByRole("heading", { name: "Le mie VPN", exact: true }),
  ).toBeVisible();
  await second
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await second
    .getByRole("dialog")
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  for (const tab of [page, second]) {
    await expect(
      tab.getByRole("heading", { name: "Accedi a FactoryVPN" }),
    ).toBeVisible();
    await expect(tab.getByText(id, { exact: true })).toHaveCount(0);
  }
  expect((await other.request.get("/api/v1/web/session")).status()).toBe(200);
  const detail = await (await call(other, `/sessions/${id}`)).json();
  expect(detail.status).toBe("CLOSED");
  expect(detail.cleaned_at).toBeNull();
  await second.close();
  await other.close();
});

test("diagnostics distinguishes missing evidence, peers and network errors and exports filtered history", async ({
  page,
  context,
  request,
}) => {
  await login(page);
  const id = await open(page);
  const step = (title: string) =>
    page.locator(".diagnostic-step").filter({
      has: page.getByRole("heading", { name: title, exact: true }),
    });
  await expect(step("Client del tecnico")).toContainText("Non verificabile");
  const detail = await (await call(context, `/sessions/${id}`)).json();
  await request.post("/api/v1/gateways/heartbeat", {
    headers: { "X-Gateway-Key": keys[identifier(20)] },
    data: { ready_session_id: id, vpn_ip: detail.gateway_vpn_ip },
  });
  const observation = (
    technician: boolean,
    network = "waiting",
    code = "none",
  ) => ({
    observed_at: new Date().toISOString(),
    gateway_connected: true,
    technician_connected: technician,
    network,
    code,
  });
  for (let n = 0; n < 3; n++) {
    expect(
      (
        await request.post(`/api/v1/internal/sessions/${id}/waiting`, {
          headers: workerHeaders,
          data: observation(false),
        })
      ).status(),
    ).toBe(200);
  }
  await expect(step("Tunnel del gateway")).toContainText("Confermato");
  await expect(step("Client del tecnico")).toContainText(
    "Il client non risulta collegato",
  );
  await expect(step("Client del tecnico")).toContainText(
    "Avviare manualmente OpenVPN",
  );
  await page
    .getByRole("combobox", { name: "Tipo di evento", exact: true })
    .selectOption("worker_changed");
  await page
    .getByRole("button", { name: "Filtra eventi", exact: true })
    .click();
  await expect(
    page
      .getByRole("table")
      .getByText("Osservazione del server VPN", { exact: true }),
  ).toHaveCount(1);
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Esporta CSV", exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe("factoryvpn-session-events.csv");
  const stream = await download.createReadStream();
  let csv = "";
  for await (const chunk of stream!) csv += chunk.toString();
  expect(csv).toContain(id);
  expect(csv).toContain("worker_changed");
  expect(csv).not.toContain("session_requested");
  await request.post(`/api/v1/internal/sessions/${id}/diagnostics`, {
    headers: workerHeaders,
    data: observation(true, "error", "network_failed"),
  });
  await expect(step("Rete di assistenza")).toContainText("Problema segnalato");
  await expect(
    page.getByRole("heading", {
      name: "Problema di collegamento segnalato",
      exact: true,
    }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/diagnostics-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/diagnostics-mobile.png",
    fullPage: true,
  });
});

test("history and audit exports retain group scope with gateway and session filters", async ({
  page,
  context,
  browser,
}) => {
  await login(page);
  const north = await open(page);
  const other = await browser.newContext({ baseURL: origin });
  const southPage = await other.newPage();
  await login(southPage, "southern");
  const south = await open(southPage, "Pressa Sud");
  await page.goto(`/app/vpn-history?gateway_id=${identifier(20)}`);
  await expect(
    page.getByRole("link", { name: north, exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: south, exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Sessione (UUID)", { exact: true }).fill(south);
  await page
    .getByRole("button", { name: "Filtra eventi", exact: true })
    .click();
  await expect(
    page.getByText("Nessun evento con questi filtri", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Esporta CSV", exact: true }),
  ).toBeDisabled();
  // A delegated administrator may inspect administrative events only in the same group.
  expect(
    (
      await completeApi(context.request, await context.request.post("/api/v1/web/login", {
        headers: { Origin: origin, "X-FactoryVPN-Portal": "1" },
        data: { username: "delegate", password },
      }), "delegate", true)
    ).status(),
  ).toBe(200);
  await page.goto(`/app/audit?session_id=${north}`);
  await expect(
    page.getByText("session_requested", { exact: true }),
  ).toBeVisible();
  const exporting = page.waitForEvent("download");
  await page.getByRole("button", { name: "Esporta CSV", exact: true }).click();
  const stream = await (await exporting).createReadStream();
  let csv = "";
  for await (const chunk of stream!) csv += chunk.toString();
  expect(csv).toContain(north);
  expect(csv).not.toContain(south);
  await page.getByLabel("Sessione (UUID)", { exact: true }).fill(south);
  await page
    .getByRole("button", { name: "Filtra eventi", exact: true })
    .click();
  await expect(
    page.getByText("Nessuna attività trovata", { exact: true }),
  ).toBeVisible();
  await other.close();
});

test("an export response arriving after logout cannot download previous session data", async ({
  page,
}) => {
  await login(page);
  await open(page);
  let release!: () => void;
  let started!: () => void;
  const pending = new Promise<void>((resolve) => {
    started = resolve;
  });
  const resume = new Promise<void>((resolve) => {
    release = resolve;
  });
  let downloads = 0;
  page.on("download", () => {
    downloads++;
  });
  await page.route("**/api/v1/session-events.csv?**", async (route) => {
    const response = await route.fetch();
    started();
    await resume;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Esporta CSV", exact: true }).click();
  await pending;
  await page
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Accedi a FactoryVPN" }),
  ).toBeVisible();
  const response = page.waitForResponse("**/api/v1/session-events.csv?**");
  release();
  await response;
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  expect(downloads).toBe(0);
});
