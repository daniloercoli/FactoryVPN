import { completeEmail, completeApi } from "./email";
import { test, expect, type Page } from "./fixtures";

async function login(page: Page, username = "root") {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill("factoryvpn-browser-tests");
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(page.getByRole("heading", { name: "Accessi sotto controllo" })).toBeVisible();
}

test("global administrator can download, set retention and queue a backup; restore needs confirmation", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "Backup e ripristino" }).click();
  await expect(page.getByRole("heading", { name: "Backup e ripristino", exact: true })).toBeVisible();
  await expect(page.getByText("Coordinatore disponibile", { exact: true })).toBeVisible();
  const saving = page.waitForEvent("download");
  await page.getByRole("button", { name: "Scarica", exact: true }).click();
  expect((await saving).suggestedFilename()).toBe("factoryvpn-00000000-0000-4000-8000-000000000099.fvbackup");
  await page.getByRole("spinbutton", { name: "Copie da conservare" }).fill("4");
  const saved = page.waitForResponse(response => response.url().endsWith("/backups/retention") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Salva conservazione" }).click();
  expect((await saved).status()).toBe(204);
  await page.getByRole("button", { name: "Ripristina", exact: true }).click();
  await page.getByLabel("Scrivi RIPRISTINA per confermare").fill("NO");
  await page.getByRole("button", { name: "Conferma ripristino" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Annulla", exact: true }).last().click();
  await page.screenshot({ path: "test-results/backups-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/backups-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "Crea backup" }).click();
  await expect(page.getByRole("region", { name: "Operazioni recenti" }).getByText("Creazione backup", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Crea backup" })).toBeDisabled();
});

test("delegated administrators cannot see or directly open recovery", async ({ page }) => {
  await login(page, "delegate");
  await expect(page.getByRole("link", { name: "Backup e ripristino" })).toHaveCount(0);
  await page.goto("/app/backups");
  await expect(page.getByRole("heading", { name: "Accesso non disponibile" })).toBeVisible();
  expect(await page.evaluate(async () => (await fetch("/api/v1/backups")).status)).toBe(403);
});

test("an incorrect confirmation password keeps the portal session open", async ({ page }) => {
  await login(page);
  // Show the five-minute confirmation form without waiting in the browser test.
  let forceConfirmation = true;
  await page.route("**/api/v1/backups", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), needs_reauthentication: forceConfirmation || (await response.json()).needs_reauthentication } });
  });
  await page.goto("/app/backups");
  const form = page.getByRole("region", { name: "Conferma identità" });
  await form.getByLabel("Password attuale", { exact: true }).fill("incorrect-password");
  await form.getByRole("button", { name: "Conferma identità", exact: true }).click();
  await expect(page.getByText("Password di conferma non corretta. Riprova.")).toBeVisible();
  await expect(page).toHaveURL(/\/app\/backups$/);

  await form.getByLabel("Password attuale", { exact: true }).fill("factoryvpn-browser-tests");
  await form.getByRole("button", { name: "Conferma identità", exact: true }).click();
  await expect(page.getByLabel("Codice email", { exact: true })).toBeVisible();

  await completeEmail(page, "root", true);
  forceConfirmation = false;
  await page.reload();
  await expect(form).toHaveCount(0);
});
