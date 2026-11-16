import { completeEmail, completeApi } from "./email";
import {
  test,
  expect,
  type Page,
  type APIRequestContext,
} from "./fixtures";

const password = "factoryvpn-browser-tests";
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const origin = process.env.FACTORYVPN_PORTAL_URL!;
const input = {
  name: "PLC 100%_Nord",
  ip_address: "192.168.1.25",
  mac_address: "AA:BB:CC:DD:EE:FF",
  device_type: "PLC",
};

test("gateway identifiers can be edited and searched in both catalogs on desktop and mobile", async ({ page, request }) => {
  const headers = await root(request);
  const values = { serial_number: "GW-100%_Nord", imei: "012345678901234", iccid: "08912345678901234567" };
  try {
    await login(page);
    await page.goto(`/app/gateways/${id(20)}`);
    await page.getByRole("button", { name: "Modifica identificativi" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Matricola (facoltativo)").fill(values.serial_number);
    await dialog.getByLabel("IMEI modem (facoltativo)").fill(values.imei);
    await dialog.getByLabel("ICCID SIM (facoltativo)").fill(values.iccid);
    await dialog.getByRole("button", { name: "Salva modifiche" }).click();
    await expect(dialog).toHaveCount(0);
    const card = page.getByRole("region", { name: "Stato e rete del gateway" });
    for (const value of Object.values(values)) await expect(card).toContainText(value);
    for (const n of [21, 23]) {
      const response = await request.patch(`/api/v1/admin/gateways/${id(n)}`, { headers, data: { imei: values.imei, iccid: values.iccid } });
      expect(response.status()).toBe(200);
    }
    for (const path of ["gateways", "vpn"]) {
      await page.goto(`/app/${path}`);
      for (const term of [values.imei, "345678901234", values.iccid]) {
        await page.getByLabel("Cerca gateway").fill(term);
        await expect(page.getByText("1–2 di 2", { exact: true })).toBeVisible();
        await expect(page.getByRole("link", { name: "Pressa Nord", exact: true })).toBeVisible();
        await expect(page.getByRole("link", { name: "Pressa Sud", exact: true })).toHaveCount(0);
      }
      for (const term of ["%_", "gw-100%_nord", "02-00-00-00-00-01", "020000000001"]) {
        await page.getByLabel("Cerca gateway").fill(term);
        await expect(page.getByText("1–1 di 1", { exact: true })).toBeVisible();
        await expect(page.getByRole("row").filter({ hasText: "Pressa Nord" })).toContainText(values.iccid);
      }
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.goto(`/app/gateways/${id(20)}`);
    await page.getByRole("button", { name: "Modifica identificativi" }).click();
    await expect(dialog.getByLabel("ICCID SIM (facoltativo)")).toHaveValue(values.iccid);
    await page.screenshot({ path: "test-results/gateway-identifiers-mobile.png", fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  } finally {
    for (const n of [20, 21, 23]) {
      await request.patch(`/api/v1/admin/gateways/${id(n)}`, { headers, data: { serial_number: null, imei: null, iccid: null } });
    }
  }
});
async function login(page: Page, username = "delegate") {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
}
async function root(request: APIRequestContext) {
  const response = await completeApi(request, await request.post("/api/v1/auth/login", {
    data: { username: "root", password },
  }));
  expect(response.status()).toBe(200);
  return { Authorization: "Bearer " + (await response.json()).access_token };
}
async function add(request: APIRequestContext, n = 20) {
  const response = await request.post(
    `/api/v1/admin/gateways/${id(n)}/devices`,
    { headers: await root(request), data: input },
  );
  expect(response.status()).toBe(201);
  return (await response.json()).id as string;
}
test.afterEach(async ({ request }) => {
  const headers = await root(request);
  const rows = await (
    await request.get("/api/v1/admin/sessions?reserved=true", { headers })
  ).json();
  for (const row of rows) {
    await request.delete(`/api/v1/sessions/${row.id}`, { headers });
    expect(
      (
        await request.post(`/api/v1/internal/sessions/${row.id}/cleaned`, {
          headers: { "X-Worker-Key": process.env.WORKER_API_KEY! },
        })
      ).status(),
    ).toBe(200);
  }
  await request.put(`/api/v1/admin/gateways/${id(20)}/group`, {
    headers,
    data: { group_id: id(1) },
  });
  for (const n of [20, 21]) {
    const base = `/api/v1/admin/gateways/${id(n)}/devices`;
    const devices = await (await request.get(base, { headers })).json();
    for (const device of devices)
      expect(
        (await request.delete(`${base}/${device.id}`, { headers })).status(),
      ).toBe(204);
  }
});

test("gateway card links from lists, edits inventory with revisions, filters and works on mobile", async ({
  page,
  request,
}) => {
  await login(page);
  await page.goto("/app/gateways");
  await page.getByRole("link", { name: "Pressa Nord", exact: true }).click();
  await expect(page).toHaveURL(`/app/gateways/${id(20)}`);
  await expect(
    page.getByRole("heading", { name: "Pressa Nord", exact: true, level: 1 }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Stato e rete del gateway" }),
  ).toContainText("Qualsiasi servizio sull’IP VPN");
  await expect(page.getByText("Rete LAN", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Telemetria del gateway" }),
  ).toContainText("Mai ricevuti");
  await expect(
    page.getByRole("link", { name: "Audit del gateway" }),
  ).toHaveAttribute("href", `/app/audit?gateway_id=${id(20)}`);
  await page.getByRole("button", { name: "Aggiungi dispositivo" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome dispositivo").fill(input.name);
  await dialog.getByLabel("Indirizzo IPv4").fill(input.ip_address);
  await dialog.getByLabel("Indirizzo MAC").fill(input.mac_address);
  await dialog
    .getByRole("combobox", { name: "Tipo", exact: true })
    .selectOption("PLC");
  await dialog.getByRole("button", { name: "Salva dispositivo" }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByLabel("Cerca dispositivi").fill("%_");
  await expect(
    page.getByRole("row").filter({ hasText: input.name }),
  ).toBeVisible();
  await expect(page.getByText("1–1 di 1")).toBeVisible();
  await page
    .getByRole("combobox", { name: "Tipo dispositivo", exact: true })
    .selectOption("HMI");
  await expect(
    page.getByText("Nessun dispositivo con questi filtri"),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Tipo dispositivo", exact: true })
    .selectOption("");
  await page.getByLabel("Cerca dispositivi").fill("");
  await page
    .getByRole("button", { name: `Modifica ${input.name}`, exact: true })
    .click();
  const headers = await root(request);
  const base = `/api/v1/admin/gateways/${id(20)}/devices`;
  const device = (await (await request.get(base, { headers })).json())[0];
  expect(
    (
      await request.put(`${base}/${device.id}`, {
        headers,
        data: { ...input, name: "Modificato altrove" },
      })
    ).status(),
  ).toBe(200);
  await dialog.getByLabel("Nome dispositivo").fill("Modulo obsoleto");
  await dialog.getByRole("button", { name: "Salva dispositivo" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Questi dati sono cambiati",
  );
  await dialog.getByRole("button", { name: "Ricarica inventario" }).click();
  await page
    .getByRole("button", { name: "Modifica Modificato altrove", exact: true })
    .click();
  await dialog.getByLabel("Nome dispositivo").fill("HMI pressa");
  await dialog
    .getByRole("combobox", { name: "Tipo", exact: true })
    .selectOption("HMI");
  await dialog.getByRole("button", { name: "Salva dispositivo" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("row").filter({ hasText: "HMI pressa" }),
  ).toContainText("Mai");
  await page
    .getByRole("heading", { name: "Pressa Nord", exact: true, level: 1 })
    .click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/gateway-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/gateway-mobile.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Rimuovi HMI pressa", exact: true })
    .click();
  await dialog.getByRole("button", { name: "Rimuovi dall’inventario" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("Inventario vuoto", { exact: true }),
  ).toBeVisible();
});

test("technician reads inventory and opens a VPN from its card without management or telemetry", async ({
  page,
  request,
  browser,
}) => {
  await add(request);
  await login(page, "technician");
  await page.goto("/app/vpn");
  await page.getByRole("link", { name: "Pressa Nord", exact: true }).click();
  await expect(
    page.getByRole("row").filter({ hasText: input.name }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Aggiungi dispositivo" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Modifica nome" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("region", { name: "Telemetria del gateway" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Audit del gateway" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Cronologia dei tuoi interventi" }),
  ).toHaveAttribute("href", `/app/vpn-history?gateway_id=${id(20)}`);
  await page
    .getByRole("button", { name: "Apri sessione VPN", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(page).toHaveURL(/\/app\/vpn\/[0-9a-f-]{36}$/);
  const session = page.url().split("/").pop()!;
  await page.getByRole("link", { name: "Scheda gateway", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Segui sessione" }),
  ).toHaveAttribute("href", `/app/vpn/${session}`);
  await expect(
    page.getByText("Attesa del gateway Wi-Fi", { exact: true }),
  ).toBeVisible();
  const otherContext = await browser.newContext({ baseURL: origin });
  const other = await otherContext.newPage();
  await login(other, "shared");
  await other.goto(`/app/gateways/${id(20)}`);
  await expect(
    other.getByRole("heading", { name: "Occupato", exact: true }),
  ).toBeVisible();
  await expect(other.getByRole("link", { name: "Segui sessione" })).toHaveCount(
    0,
  );
  expect(await other.locator("body").innerText()).not.toContain(session);
  await otherContext.close();
  await page.goto(`/app/gateways/${id(21)}`);
  await expect(
    page.getByRole("heading", { name: "Gateway non disponibile" }),
  ).toBeVisible();
  await expect(page.getByText("Pressa Sud", { exact: true })).toHaveCount(0);
});

test("gateway move hides the whole card, forms and a pending inventory response", async ({
  page,
  request,
}) => {
  await page.clock.install();
  await add(request);
  await login(page);
  await page.goto(`/app/gateways/${id(20)}`);
  await expect(
    page.getByRole("row").filter({ hasText: input.name }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: `Modifica ${input.name}`, exact: true })
    .click();
  let release!: () => void;
  let received!: () => void;
  const waiting = new Promise<void>((resolve) => {
    received = resolve;
  });
  const resume = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(`**/api/v1/devices/gateway/${id(20)}?*`, async (route) => {
    const response = await route.fetch();
    received();
    await resume;
    await route.fulfill({ response }).catch(() => {});
  });
  // Start a background read before transferring ownership, keeping the form open.
  await page.clock.fastForward(16000);
  await waiting;
  const headers = await root(request);
  expect(
    (
      await request.put(`/api/v1/admin/gateways/${id(20)}/group`, {
        headers,
        data: { group_id: id(2) },
      })
    ).status(),
  ).toBe(200);
  await page.clock.fastForward(11000);
  await expect(
    page.getByRole("heading", { name: "Gateway non disponibile" }),
  ).toBeVisible();
  release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Inventario dispositivi" }),
  ).toHaveCount(0);
  await expect(page.getByText(input.name, { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Telemetria del gateway" }),
  ).toHaveCount(0);
});

test("revocation removes an open inventory form and prevents writes", async ({
  page,
  request,
}) => {
  const headers = await root(request);
  const created = await request.post("/api/v1/admin/users", {
    headers,
    data: {
      username: "gateway-editor",
      email: "gateway-editor@example.test",
      password,
      permissions: ["gateways.manage"],
      group_ids: [id(1)],
    },
  });
  expect(created.status()).toBe(201);
  const user = await created.json();
  await login(page, "gateway-editor");
  await page.goto(`/app/gateways/${id(20)}`);
  await page.getByRole("button", { name: "Aggiungi dispositivo" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome dispositivo").fill("Scrittura revocata");
  await dialog.getByLabel("Indirizzo IPv4").fill(input.ip_address);
  await dialog.getByLabel("Indirizzo MAC").fill(input.mac_address);
  expect(
    (
      await request.put(`/api/v1/admin/users/${user.id}`, {
        headers,
        data: { permissions: [] },
      })
    ).status(),
  ).toBe(200);
  await dialog.getByRole("button", { name: "Salva dispositivo" }).click();
  await expect(
    page.getByRole("heading", { name: "Accedi a FactoryVPN" }),
  ).toBeVisible();
  await expect(dialog).toHaveCount(0);
  const rows = await (
    await request.get(`/api/v1/admin/gateways/${id(20)}/devices`, { headers })
  ).json();
  expect(rows).toHaveLength(0);
  expect(
    (
      await request.delete(`/api/v1/admin/users/${user.id}`, { headers })
    ).status(),
  ).toBe(204);
});

test("unassigned and unconfigured gateway states are distinct from SMS waiting", async ({
  page,
}) => {
  await login(page, "root");
  await page.goto(`/app/gateways/${id(22)}`);
  await expect(
    page.getByRole("heading", { name: "Non assegnato", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Apri sessione VPN" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Cambia gruppo" }).click();
  await page
    .getByRole("dialog")
    .getByLabel("Gruppo di destinazione")
    .selectOption(id(1));
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Salva modifiche" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Da configurare", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Apri sessione VPN" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Cambia gruppo" }).click();
  await page
    .getByRole("dialog")
    .getByLabel("Gruppo di destinazione")
    .selectOption("");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Salva modifiche" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.goto(`/app/gateways/${id(23)}`);
  await page.getByRole("button", { name: "Apri sessione VPN" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Apri sessione", exact: true })
    .click();
  await expect(page).toHaveURL(/\/app\/vpn\/[0-9a-f-]{36}$/);
  await page.getByRole("link", { name: "Scheda gateway", exact: true }).click();
  await expect(
    page.getByText("Attesa ricezione SMS", { exact: true }),
  ).toBeVisible();
});

for (const failure of [503, 404]) {
  test(`inventory refresh error ${failure} preserves edits only while access is available`, async ({
    page,
    request,
  }) => {
    await page.clock.install();
    await add(request);
    await login(page);
    await page.goto(`/app/gateways/${id(20)}`);
    await page
      .getByRole("button", { name: `Modifica ${input.name}`, exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog
      .getByLabel("Nome dispositivo")
      .fill("Modifica non ancora salvata");
    let unavailable = true;
    await page.route(`**/api/v1/devices/gateway/${id(20)}?*`, async (route) => {
      if (unavailable) {
        await route.fulfill({
          status: failure,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Temporary inventory read failure" }),
        });
      } else await route.continue();
    });
    await page.clock.fastForward(16000);
    if (failure === 404) {
      await expect(dialog).toHaveCount(0);
    } else {
      await expect(dialog.getByRole("alert")).toBeVisible();
      await expect(dialog.getByLabel("Nome dispositivo")).toHaveValue(
        "Modifica non ancora salvata",
      );
    }
    unavailable = false;
    await page.clock.fastForward(16000);
    await expect(
      page
        .getByRole("region", { name: "Inventario dispositivi" })
        .getByRole("alert"),
    ).toHaveCount(0);
    if (failure === 404) {
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByRole("row").filter({ hasText: input.name }),
      ).toBeVisible();
      return;
    }
    await expect(dialog.getByLabel("Nome dispositivo")).toHaveValue(
      "Modifica non ancora salvata",
    );
    await dialog.getByRole("button", { name: "Salva dispositivo" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("row").filter({ hasText: "Modifica non ancora salvata" }),
    ).toBeVisible();
  });
}
