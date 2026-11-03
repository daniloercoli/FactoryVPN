import { test, expect } from "./fixtures";
import { completeEmail, completeApi, codeFor } from "./email";
const password = "factoryvpn-browser-tests";

test("administrator must verify local SMTP code before accessing the portal", async ({ page, context }) => {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill("root");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(page.getByLabel("Codice email", { exact: true })).toBeVisible();
  expect((await context.request.get("/api/v1/web/session")).status()).toBe(401);
  const correct = await codeFor(page.request, "root");
  await page.getByLabel("Codice email", { exact: true }).fill(correct === "000000" ? "111111" : "000000");
  await page.getByRole("button", { name: "Verifica codice", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Codice errato");
  expect((await context.request.get("/api/v1/web/session")).status()).toBe(401);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/email-login-mobile.png", fullPage: true });
  await completeEmail(page, "root");
  await expect(page.getByRole("heading", { name: "Accessi sotto controllo" })).toBeVisible();
  await page.goto("/app/profile");
  await expect(page.getByText("Obbligatoria per amministratori")).toBeVisible();
  expect(await page.getByRole("combobox", { name: "Operazione", exact: true }).locator("option").allTextContents()).not.toContain("Disattiva verifica in due passaggi");
});

test("technician enables email verification and changes address with both codes", async ({ page, request }) => {
  const root = await completeApi(request, await request.post("/api/v1/auth/login", { data: { username: "root", password } }));
  const headers = { Authorization: "Bearer " + (await root.json()).access_token };
  const created = await request.post("/api/v1/admin/users", { headers, data: {
    username: "email-technician", email: "email-technician@example.test", password,
    group_ids: ["00000000-0000-4000-8000-000000000001"],
  } });
  expect(created.status()).toBe(201);
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill("email-technician");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Accessi sotto controllo" })).toBeVisible();
  await page.goto("/app/profile");
  await page.getByRole("combobox", { name: "Operazione", exact: true }).selectOption("enable");
  await page.getByLabel("Password di conferma", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Invia codice email", exact: true }).click();
  await completeEmail(page, "email-technician", true);
  await expect(page.getByText("Attiva", { exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Operazione", exact: true }).selectOption("change");
  await page.getByLabel("Nuovo indirizzo email", { exact: true }).fill("updated-email@example.test");
  await page.getByLabel("Password di conferma", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Invia codice email", exact: true }).click();
  await completeEmail(page, "email-technician", true);
  await expect(page.getByText("u***@example.test", { exact: true })).toBeVisible();
  await completeEmail(page, "updated-email@example.test", true);
  await expect(page.getByLabel("Email", { exact: true })).toHaveValue("updated-email@example.test");
  await page.screenshot({ path: "test-results/email-profile-desktop.png", fullPage: true });
});
