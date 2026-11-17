import { appName } from "./branding";
import { completeEmail, completeApi } from "./email";
import { test, expect, type Page, type APIRequestContext } from "./fixtures";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
const password = "factoryvpn-browser-tests";
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
async function login(page: Page, username: string) {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(page.getByRole("heading", { name: "Accessi sotto controllo" })).toBeVisible();
}
async function root(request: APIRequestContext) {
  const response = await completeApi(request, await request.post("/api/v1/auth/login", { data: { username: "root", password } }));
  return { Authorization: "Bearer " + (await response.json()).access_token };
}

for (const mode of ["wifi", "sms"]) {
test(`delegate provisions, downloads and activates a ${mode} gateway; registration verifies configuration`, async ({ page, request }) => {
  await login(page, "delegate");
  await page.goto("/app/vpn");
  await page.getByRole("link", { name: "Aggiungi gateway" }).click();
  await page.getByLabel("Nome", { exact: true }).fill("Gateway provisioning test");
  await page.getByLabel("Matricola (facoltativo)").fill(`FACTORY-${mode}`);
  await page.getByLabel("IMEI modem (facoltativo)").fill("012345678901234");
  await page.getByLabel("ICCID SIM (facoltativo)").fill("08912345678901234567");
  await page.getByRole("combobox", { name: /^Gruppo/ }).selectOption(id(1));
  await page.getByRole("combobox", { name: /^Attivazione/ }).selectOption(mode);
  if (mode === "wifi") {
    await expect(page.getByLabel("MAC registrato", { exact: true })).toHaveAttribute("required", "");
    await page.getByLabel("MAC registrato", { exact: true }).fill("02:AC:11:22:33:44");
  } else {
    await expect(page.getByLabel("MAC registrato (facoltativo)", { exact: true })).not.toHaveAttribute("required", "");
    await page.getByLabel("Numero telefono", { exact: true }).fill("+393339990001");
  }
  await page.getByRole("button", { name: "Prepara gateway" }).click();
  await expect(page).toHaveURL(/\/app\/gateways\/[0-9a-f-]{36}$/);
  const gatewayId = page.url().split("/").at(-1)!;
  try {
    const card = page.getByRole("region", { name: "Stato e rete del gateway" });
    await expect(card).toContainText(`FACTORY-${mode}`);
    await expect(card).toContainText("012345678901234");
    await expect(card).toContainText("08912345678901234567");
    const section = page.getByRole("region", { name: "Credenziali del gateway" });
    const downloading = page.waitForEvent("download");
    await section.getByRole("button", { name: "Scarica pacchetto gateway" }).click();
    const file = await downloading;
    expect(file.suggestedFilename()).toBe(`factoryvpn-gateway-${gatewayId}.zip`);
    const env = execFileSync("../.venv/bin/python", ["-c", "import sys,zipfile; print(zipfile.ZipFile(sys.argv[1]).read('gateway.env').decode())", (await file.path())!], { encoding: "utf8" });
    const config = Object.fromEntries(env.trim().split("\n").map(line => line.split("=")));
    expect(config.GATEWAY_API_KEY).not.toBe(config.GATEWAY_SMS_KEY);
    expect(config).not.toHaveProperty("LAN_CIDR");
    expect(config).not.toHaveProperty("LAN_ADDRESS");
    expect(config).not.toHaveProperty("LAN_INTERFACE");
    expect(config.MAC_ADDRESS).toBe(mode === "wifi" ? "02:AC:11:22:33:44" : "");
    await section.getByRole("button", { name: "Attiva il nuovo profilo" }).click();
    await page.getByRole("button", { name: "Ho salvato il file: attiva" }).click();
    await expect(section.getByText("Certificato attivo", { exact: true })).toBeVisible();
    const registration = await request.post("/api/v1/gateways/register", {
      headers: { "X-Gateway-Key": config.GATEWAY_API_KEY }, data: {
        gateway_id: gatewayId, activation_mode: mode, phone_number: config.GATEWAY_PHONE_NUMBER || null,
        mac_address: config.MAC_ADDRESS || null,
      },
    });
    expect(registration.status()).toBe(200);
    const registered = await registration.json();
    await page.reload();
    await expect(card).toContainText("Indirizzo VPN fisso");
    await expect(card).toContainText(registered.vpn_ip);
    await expect(section.getByText(/Registrazione confermata/)).toBeVisible();
    await section.getByRole("button", { name: "Richiedi rinnovo remoto" }).click();
    await expect(section.getByText("Rinnovo remoto del solo certificato: richiesto.")).toBeVisible();
    await section.getByRole("button", { name: "Revoca credenziali VPN" }).click();
    await page.getByRole("button", { name: "Revoca e chiudi le VPN" }).click();
    await expect(section.getByText("Nessun certificato attivo.")).toBeVisible();
    expect((await request.post("/api/v1/gateways/heartbeat", { headers: { "X-Gateway-Key": config.GATEWAY_API_KEY }, data: {} })).status()).toBe(401);
  } finally {
    expect((await request.delete(`/api/v1/admin/gateways/${gatewayId}`, { headers: await root(request) })).status()).toBe(204);
  }
});
}

test("technician obtains only their profile and activates it after saving the file", async ({ page, request }) => {
  const headers = await root(request);
  const username = "profile-" + randomUUID().slice(0, 8);
  const created = await request.post("/api/v1/admin/users", { headers, data: { username, password, permissions: ["connect"], group_ids: [id(1)] } });
  expect(created.status()).toBe(201);
  const user = await created.json();
  try {
    await login(page, username);
    await page.goto("/app/profile");
    const section = page.getByRole("region", { name: "Profilo VPN personale" });
    await section.getByRole("button", { name: "Prepara profilo VPN" }).click();
    const downloading = page.waitForEvent("download");
    await section.getByRole("button", { name: "Scarica profilo VPN" }).click();
    expect((await downloading).suggestedFilename()).toBe(`factoryvpn-technician-${user.id}.ovpn`);
    await section.getByRole("button", { name: "Attiva il nuovo profilo" }).click();
    await page.getByRole("button", { name: "Ho salvato il file: attiva" }).click();
    await expect(section.getByText("Certificato attivo", { exact: true })).toBeVisible();
    await page.goto("/app/gateways/new");
    await expect(page.getByRole("heading", { name: "Accesso non disponibile" })).toBeVisible();
  } finally {
    await request.delete(`/api/v1/admin/users/${user.id}`, { headers });
  }
});

test("SSO profile delivery is discarded when its browser logs out during download", async ({ page }) => {
  await page.goto("/app/login");
  await page.getByRole("button", { name: "Provider di test" }).click();
  await page.getByRole("button", { name: "Continua come utente SSO" }).click();
  await expect(page.getByRole("heading", { name: "Accessi sotto controllo" })).toBeVisible();
  await page.goto("/app/profile");
  const section = page.getByRole("region", { name: "Profilo VPN personale" });
  await section.getByRole("button", { name: "Prepara rinnovo" }).click();
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  let captured!: () => void;
  const started = new Promise<void>(resolve => { captured = resolve; });
  let downloads = 0;
  page.on("download", () => downloads++);
  await page.route("**/provisioning/jobs/*/download", async route => {
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    captured();
    await waiting;
    await route.fulfill({ response });
  });
  await section.getByRole("button", { name: "Scarica profilo VPN" }).click();
  await started;
  await page.getByRole("button", { name: "Esci dal portale", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Esci dal portale", exact: true }).click();
  await expect(page.getByRole("heading", { name: `Accedi a ${appName}` })).toBeVisible();
  release();
  await login(page, "technician");
  expect(downloads).toBe(0);
});

test("an open revoke confirmation cannot revoke a replacement certificate from another access", async ({ page, request }) => {
  const headers = await root(request);
  await login(page, "root");
  await page.goto("/app/profile");
  const section = page.getByRole("region", { name: "Profilo VPN personale" });
  const peer = `/api/v1/provisioning/peers/technician/${id(10)}`;
  const original = (await (await request.get(peer, { headers })).json()).certificate;
  await section.getByRole("button", { name: "Revoca credenziali VPN" }).click();
  const prepared = await request.post(peer + "/prepare", {
    headers: { ...headers, "Idempotency-Key": randomUUID() },
    data: { expected_certificate_id: original.id },
  });
  expect(prepared.status()).toBe(202);
  const job = `/api/v1/provisioning/jobs/${(await prepared.json()).id}`;
  await expect.poll(async () => (await (await request.get(job, { headers })).json()).state).toBe("ready");
  expect((await request.post(job + "/download", { headers })).status()).toBe(200);
  expect((await request.post(job + "/activate", { headers })).status()).toBe(200);
  const replacement = (await (await request.get(peer, { headers })).json()).certificate;
  expect(replacement.id).not.toBe(original.id);
  // Wait for the actual five-second polling to refresh the card behind the dialog.
  await expect(section.getByText(`Seriale: ${replacement.serial}`, { exact: true })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Revoca e chiudi le VPN" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Il certificato è cambiato");
  expect((await (await request.get(peer, { headers })).json()).certificate.id).toBe(replacement.id);
});
