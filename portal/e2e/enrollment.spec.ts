import { execFileSync } from "node:child_process";
import { completeEmail, completeApi } from "./email";
import { test, expect, type Page, type APIRequestContext } from "./fixtures";

const password = "factoryvpn-browser-tests";
const id = (number: number) =>
  `00000000-0000-4000-8000-${String(number).padStart(12, "0")}`;

async function login(page: Page, username = "root") {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
}

async function credentials(request: APIRequestContext, username = "root") {
  const response = await completeApi(
    request,
    await request.post("/api/v1/auth/login", { data: { username, password } }),
    username,
  );
  expect(response.status()).toBe(200);
  return { Authorization: "Bearer " + (await response.json()).access_token };
}

test.beforeEach(() => {
  execFileSync("../.venv/bin/python", ["-m", "portal_enrollment_seed"], {
    stdio: "pipe",
  });
});

test("only global admins see enrollment; manual gateway creation remains available", async ({
  page,
  request,
}) => {
  await login(page, "delegate");
  await expect(
    page.getByRole("link", { name: "Arruolamenti", exact: true }),
  ).toHaveCount(0);
  await page.goto("/app/enrollments");
  await expect(
    page.getByRole("heading", { name: "Accesso non disponibile" }),
  ).toBeVisible();
  await page.goto(`/app/enrollments/${id(801)}`);
  await expect(
    page.getByRole("heading", { name: "Accesso non disponibile" }),
  ).toBeVisible();
  const headers = await credentials(request, "delegate");
  expect(
    (await request.get("/api/v1/admin/enrollments", { headers })).status(),
  ).toBe(403);
  expect(
    (
      await request.patch(`/api/v1/admin/gateways/${id(20)}`, {
        headers,
        data: { mac_address: "02:AA:00:00:20:00" },
      })
    ).status(),
  ).toBe(403);
  await page.goto(`/app/gateways/${id(20)}`);
  await expect(
    page.getByRole("button", { name: "Modifica connessione", exact: true }),
  ).toHaveCount(0);
  await page.goto("/app/gateways/new");
  await expect(
    page.getByRole("button", { name: "Prepara gateway" }),
  ).toBeVisible();
});

test("enrollment search finds UUID, serial, phone and duplicate MAC IMEI ICCID on mobile", async ({
  page,
}) => {
  await login(page);
  await page.getByRole("link", { name: "Arruolamenti", exact: true }).click();
  await expect(page.getByText("1–3 di 3", { exact: true })).toBeVisible();
  for (const term of [
    "012345678901234",
    "08912345678901234567",
    "02-AA-00-00-08-02",
    "02aa00000802",
  ]) {
    await page.getByLabel("Cerca arruolamenti").fill(term);
    await expect(page.getByText("1–2 di 2", { exact: true })).toBeVisible();
  }
  for (const term of [id(803), "factory-duplicate", "+393339998003"]) {
    await page.getByLabel("Cerca arruolamenti").fill(term);
    await expect(page.getByText("1–1 di 1", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Arruolamento duplicati", exact: true }),
    ).toBeVisible();
  }
  await page.getByLabel("Cerca arruolamenti").fill("");
  await expect(page.getByText("1–3 di 3", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/enrollment-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/enrollment-mobile.png",
    fullPage: true,
  });
  await page
    .getByRole("link", { name: "Arruolamento duplicati", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Stato arruolamento" }),
  ).toContainText("012345678901234");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/enrollment-detail-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({
    path: "test-results/enrollment-detail-desktop.png",
    fullPage: true,
  });
});

test("admin approves a device with inactive SIM, edits data and handles stale revisions", async ({
  page,
  request,
}) => {
  await login(page);
  await page.goto(`/app/enrollments/${id(801)}`);
  const section = page.getByRole("region", { name: "Stato arruolamento" });
  await expect(section).toContainText("numero di telefono per SMS");
  await expect(section).toContainText("mesi dopo");
  await expect(
    page.getByRole("button", { name: "Passa alla gestione manuale" }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Approva e assegna", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Gruppo di destinazione").selectOption(id(1));
  await dialog
    .getByRole("button", { name: "Approva e assegna", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(section).toContainText("Approvato · dati incompleti");
  const headers = await credentials(request);
  expect(
    (
      await (
        await request.get(`/api/v1/admin/enrollments/${id(801)}`, { headers })
      ).json()
    ).job,
  ).toBeNull();
  await page
    .getByRole("button", { name: "Modifica dati", exact: true })
    .click();
  const response = await request.get(`/api/v1/admin/enrollments/${id(801)}`, {
    headers,
  });
  const current = await response.json();
  expect(
    (
      await request.patch(`/api/v1/admin/enrollments/${id(801)}`, {
        headers: { ...headers, "If-Match": `"${current.revision}"` },
        data: { display_name: "Nome aggiornato altrove" },
      })
    ).status(),
  ).toBe(200);
  await dialog.getByLabel("Numero di telefono").fill("+393339998001");
  await dialog.getByRole("button", { name: "Salva dati", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Questi dati sono cambiati",
  );
  await dialog.getByRole("button", { name: "Ricarica arruolamento" }).click();
  await expect(
    page.getByRole("heading", { name: "Nome aggiornato altrove", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Modifica dati", exact: true })
    .click();
  await dialog.getByLabel("Numero di telefono").fill("+393339998001");
  await dialog
    .getByLabel("Matricola", { exact: true })
    .fill("E2E-FACTORY-CORRECTED-001");
  await dialog.getByRole("button", { name: "Salva dati", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(section).toContainText("E2E-FACTORY-CORRECTED-001");
  await expect(section).toContainText("Approvato · attesa richiesta");
  await page
    .getByRole("button", { name: "Blocca dispositivo", exact: true })
    .click();
  await dialog
    .getByLabel("Motivo del blocco")
    .fill("Dispositivo fermato per verifica in sede");
  await dialog
    .getByRole("button", { name: "Blocca dispositivo", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(section).toContainText("Bloccato");
  await expect(section).toContainText(
    "Dispositivo fermato per verifica in sede",
  );
});

test("manual takeover reuses the device, can resume, then downloads and activates the ZIP", async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
  await login(page);
  const enrollment = `/api/v1/admin/enrollments/${id(802)}`;
  await page.goto(`/app/enrollments/${id(802)}`);
  const dialog = page.getByRole("dialog");
  await page
    .getByRole("button", { name: "Approva e assegna", exact: true })
    .click();
  await dialog.getByLabel("Gruppo di destinazione").selectOption(id(1));
  await dialog
    .getByRole("button", { name: "Approva e assegna", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const headers = await credentials(request);
  const approved = await (await request.get(enrollment, { headers })).json();
  expect(
    (
      await request.post(`/api/v1/gateway-enrollment/${id(802)}/package`, {
        headers: { "X-Enrollment-Key": "browser-secret-" + id(802) },
        data: { configuration_version: approved.configuration_version },
      })
    ).status(),
  ).toBe(202);
  await page.goto(`/app/gateways/${id(802)}`);
  const automatic = page.getByRole("region", {
    name: "Credenziali del gateway",
  });
  await expect(
    automatic.getByRole("link", { name: "Gestisci arruolamento" }),
  ).toBeVisible();
  await expect(
    automatic.getByRole("button", { name: "Scarica pacchetto gateway" }),
  ).toHaveCount(0);
  await expect(
    automatic.getByRole("button", { name: "Prepara profilo VPN" }),
  ).toHaveCount(0);
  await automatic.getByRole("link", { name: "Gestisci arruolamento" }).click();
  await page
    .getByRole("button", { name: "Passa alla gestione manuale" })
    .click();
  await dialog.getByLabel("Gruppo di destinazione").selectOption(id(1));
  await dialog
    .getByRole("button", { name: "Prepara pacchetto manuale" })
    .click();
  await expect(page).toHaveURL(`/app/gateways/${id(802)}`);
  await page.goto(`/app/enrollments/${id(802)}`);
  await page
    .getByRole("button", { name: "Riprendi arruolamento automatico" })
    .click();
  await dialog
    .getByRole("button", { name: "Riprendi arruolamento automatico" })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Passa alla gestione manuale" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Passa alla gestione manuale" })
    .click();
  await dialog
    .getByRole("button", { name: "Prepara pacchetto manuale" })
    .click();
  await expect(page).toHaveURL(`/app/gateways/${id(802)}`);
  const section = page.getByRole("region", { name: "Credenziali del gateway" });
  const downloading = page.waitForEvent("download");
  await section
    .getByRole("button", { name: "Scarica pacchetto gateway" })
    .click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe(
    `factoryvpn-gateway-${id(802)}.zip`,
  );
  await section
    .getByRole("button", { name: "Attiva il nuovo profilo" })
    .click();
  await dialog
    .getByRole("button", { name: "Ho salvato il file: attiva" })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    section.getByText("Certificato attivo", { exact: true }),
  ).toBeVisible();
  const completed = await (await request.get(enrollment, { headers })).json();
  expect(completed.id).toBe(id(802));
  expect(completed.gateway_id).toBe(id(802));
  expect(completed.state).toBe("completed");
  await page.goto(`/app/enrollments/${id(802)}`);
  await expect(
    page.getByRole("button", { name: "Riprendi arruolamento automatico" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Apri scheda gateway" }),
  ).toBeVisible();
  const peer = `/api/v1/provisioning/peers/gateway/${id(802)}`;
  const certificateBefore = (
    await (await request.get(peer, { headers })).json()
  ).certificate;
  await page
    .getByRole("button", { name: "Modifica dati", exact: true })
    .click();
  await expect(dialog.getByLabel("Modalità di attivazione")).toHaveCount(0);
  await expect(dialog.getByLabel("Numero di telefono")).toHaveCount(0);
  await expect(dialog.getByLabel("MAC registrato")).toHaveCount(0);
  await dialog
    .getByLabel("Matricola", { exact: true })
    .fill("E2E-FACTORY-CORRECTED-002");
  await dialog
    .getByLabel("Riferimento esterno (facoltativo)")
    .fill("Revisione ordine cliente");
  const saving = page.waitForRequest(
    (item) => item.method() === "PATCH" && item.url().endsWith(enrollment),
  );
  await dialog.getByRole("button", { name: "Salva dati", exact: true }).click();
  const sent = (await saving).postDataJSON();
  for (const field of ["activation_mode", "phone_number", "mac_address"])
    expect(sent).not.toHaveProperty(field);
  await expect(dialog).toHaveCount(0);
  const details = page.getByRole("region", { name: "Stato arruolamento" });
  await expect(details).toContainText("Revisione ordine cliente");
  await expect(details).toContainText("E2E-FACTORY-CORRECTED-002");
  const certificateAfter = (await (await request.get(peer, { headers })).json())
    .certificate;
  expect(certificateAfter.id).toBe(certificateBefore.id);
});

test("HQ connection change revokes old credentials and preserves gateway identity", async ({
  page,
  request,
}) => {
  const headers = await credentials(request);
  const path = `/api/v1/admin/gateways/${id(20)}`;
  const original = await (await request.get(path, { headers })).json();
  try {
    await login(page);
    await page.goto(`/app/gateways/${id(20)}`);
    await page
      .getByRole("button", { name: "Modifica connessione", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Modalità di attivazione").selectOption("sms");
    await dialog.getByLabel("Numero di telefono").fill("+393339998020");
    await dialog.getByLabel("MAC registrato").fill("02:AA:00:00:20:00");
    await dialog
      .getByRole("button", { name: "Salva e revoca il pacchetto precedente" })
      .click();
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: "Stato e rete del gateway" }),
    ).toContainText("+393339998020");
    await expect(
      page.getByRole("region", { name: "Credenziali del gateway" }),
    ).toContainText("Nessun certificato attivo");
    const updated = await (await request.get(path, { headers })).json();
    expect(updated.id).toBe(original.id);
    expect(updated.vpn_ip).toBe(original.vpn_ip);
    expect(updated.serial_number).toBe(original.serial_number);
    expect(updated.activation_mode).toBe("sms");
  } finally {
    expect(
      (
        await request.patch(path, {
          headers,
          data: {
            activation_mode: original.activation_mode,
            phone_number: original.phone_number,
            mac_address: original.mac_address,
          },
        })
      ).status(),
    ).toBe(200);
  }
});
