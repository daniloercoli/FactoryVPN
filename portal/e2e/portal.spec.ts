import { completeEmail, completeApi } from "./email";
import { test, expect, type Page, type BrowserContext } from "./fixtures";
const password = "factoryvpn-browser-tests";
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const origin = process.env.FACTORYVPN_PORTAL_URL!;

async function login(page: Page, username = "root", secret = password) {
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(secret);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, username);
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
  await expect(page).toHaveURL(origin + "/app/");
}
async function call(
  context: BrowserContext,
  path: string,
  method = "GET",
  data?: unknown,
  revision?: string,
) {
  const session = await context.request.get("/api/v1/web/session");
  const meta = session.ok() ? await session.json() : {};
  return context.request.fetch("/api/v1" + path, {
    method,
    data,
    headers: {
      Origin: origin,
      "X-FactoryVPN-Portal": "1",
      "X-CSRF-Token": meta.csrf_token || "",
      ...(revision ? { "If-Match": `"${revision}"` } : {}),
    },
  });
}
async function create(context: BrowserContext, username: string) {
  const response = await call(context, "/admin/users", "POST", {
    username,
    password,
    group_ids: [id(1)],
  });
  expect(response.status()).toBe(201);
  return response.json();
}
async function currentLogout(page: Page) {
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
}

test("local session uses HttpOnly cookies, rejects CSRF, and supports direct routes", async ({
  page,
  context,
}) => {
  await login(page);
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === "factoryvpn_portal");
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Lax");
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    "factoryvpn_portal",
  );
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  const missing = await context.request.patch("/api/v1/auth/me", {
    data: { full_name: "forged" },
    headers: { Origin: origin },
  });
  expect(missing.status()).toBe(403);
  const cross = await context.request.get("/api/v1/web/session", {
    headers: { Origin: "https://outside.test" },
  });
  expect(cross.status()).toBe(403);
  await page.goto("/app/users");
  await expect(
    page.getByRole("heading", { name: "Utenti", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Apri technician", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/users-desktop.png",
    fullPage: true,
  });
  await page.getByLabel("Cerca utenti").fill("southern");
  await expect(
    page.getByRole("link", { name: "Apri southern", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Apri technician", exact: true }),
  ).toHaveCount(0);
});

test("user creation, access changes, stale edits and deletion", async ({
  page,
  context,
}) => {
  await login(page);
  await page.goto("/app/users/new");
  await page.getByLabel("Username", { exact: true }).fill("browser-created");
  await page.getByLabel("Nome completo").fill("Utente browser <script>");
  await page.getByLabel("Password iniziale").fill(password);
  await page.getByLabel("Produzione Nord").check();
  await page.getByRole("button", { name: "Crea utente", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "browser-created", exact: true }),
  ).toBeVisible();
  const target = page.url().split("/").pop()!;
  await page.getByLabel("Gestione gruppi").check();
  await page
    .getByRole("button", { name: "Salva modifiche", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Salva e revoca gli accessi" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const snapshot = await (await call(context, `/admin/users/${target}`)).json();
  expect(snapshot.permissions).toContain("groups.manage");
  // Edit behind the open form, as another administrator or a legacy Bearer client could.
  const changed = await call(
    context,
    `/admin/users/${target}`,
    "PUT",
    { full_name: "Aggiornato altrove" },
    snapshot.revision,
  );
  expect(changed.status()).toBe(200);
  await page.getByLabel("Nome completo").fill("Vecchio modulo");
  await page
    .getByRole("button", { name: "Salva modifiche", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Questi dati sono cambiati",
  );
  await page
    .getByRole("button", { name: "Ricarica i dati", exact: true })
    .click();
  await expect(page.getByLabel("Nome completo")).toHaveValue(
    "Aggiornato altrove",
  );
  await page
    .getByRole("button", { name: "Elimina utente", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Elimina utente", exact: true })
    .click();
  await expect(page).toHaveURL(origin + "/app/users");
  expect((await call(context, `/admin/users/${target}`)).status()).toBe(404);
});

test("delegated administrator cannot see or mutate other groups or grant global rights", async ({
  page,
  context,
}) => {
  await login(page, "delegate");
  await page.goto("/app/users");
  await expect(
    page.getByRole("link", { name: "Apri technician", exact: true }),
  ).toBeVisible();
  for (const name of ["southern", "shared", "root"])
    await expect(
      page.getByRole("link", { name: `Apri ${name}`, exact: true }),
    ).toHaveCount(0);
  await expect(
    page
      .getByLabel("Gruppo", { exact: true })
      .getByRole("option", { name: "Produzione Sud" }),
  ).toHaveCount(0);
  expect(
    (await call(context, "/admin/users?group_id=" + id(2))).headers()[
      "x-total-count"
    ],
  ).toBe("0");
  expect((await call(context, "/admin/users/" + id(14))).status()).toBe(404);
  const user = await (await call(context, "/admin/users/" + id(12))).json();
  expect(
    (
      await call(
        context,
        "/admin/users/" + id(12),
        "PUT",
        { is_admin: true },
        user.revision,
      )
    ).status(),
  ).toBe(403);
  expect(
    (
      await call(
        context,
        "/admin/users/" + id(12),
        "PUT",
        { group_ids: [id(2)] },
        user.revision,
      )
    ).status(),
  ).toBe(404);
  await page.goto("/app/users/new");
  await expect(page.getByLabel("Amministratore globale")).toHaveCount(0);
  await expect(page.getByLabel("Metodo di accesso")).toHaveCount(0);
  await page.goto("/app/gateways");
  await expect(
    page.getByText("02:00:00:00:00:01", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("02:00:00:00:00:02", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("02:00:00:00:00:03", { exact: true }),
  ).toHaveCount(0);
});

test("group lifecycle and gateway assignment use current revisions", async ({
  page,
}) => {
  await login(page);
  await page.goto("/app/groups/new");
  await page.getByLabel("Nome del gruppo").fill("Gruppo browser");
  await page
    .getByLabel("Descrizione")
    .fill("Gruppo temporaneo per il collaudo");
  await page.getByRole("button", { name: "Crea gruppo", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Gruppo browser", exact: true }),
  ).toBeVisible();
  await page
    .locator(".group-row")
    .filter({ hasText: "Gruppo browser" })
    .getByRole("link", { name: "Gestisci" })
    .click();
  await page.getByLabel("Nome del gruppo").fill("Gruppo browser aggiornato");
  await page.getByRole("button", { name: "Salva gruppo" }).click();
  await page
    .locator(".group-row")
    .filter({ hasText: "Gruppo browser aggiornato" })
    .getByRole("link", { name: "Gestisci" })
    .click();
  await page
    .getByRole("button", { name: "Elimina gruppo", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Elimina gruppo", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Gruppi", exact: true }),
  ).toBeVisible();
  await page.goto("/app/gateways");
  const gateway = page
    .getByRole("row")
    .filter({ hasText: "02:00:00:00:00:03" });
  await gateway.getByRole("button", { name: "Cambia gruppo" }).click();
  await page.getByLabel("Gruppo di destinazione").selectOption(id(1));
  await page.getByRole("button", { name: "Conferma assegnazione" }).click();
  await expect(gateway).toContainText("Produzione Nord");
  await gateway.getByRole("button", { name: "Cambia gruppo" }).click();
  await page.getByLabel("Gruppo di destinazione").selectOption("");
  await page.getByRole("button", { name: "Conferma assegnazione" }).click();
  await expect(gateway).toContainText("Non assegnato");
});

test("identity links are explicit and authentication conversion is separate", async ({
  page,
  context,
}) => {
  await login(page);
  const target = await create(context, "identity-browser");
  await page.goto("/app/users/" + target.id);
  await page.getByLabel("Provider SSO").selectOption("test");
  await page.getByLabel("Subject dell’identità").fill("identity-admin-test");
  await page
    .getByRole("button", { name: "Collega identità", exact: true })
    .click();
  await expect(
    page.getByText("identity-admin-test", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Passa a SSO", exact: true }).click();
  await page
    .getByRole("button", { name: "Cambia e revoca", exact: true })
    .click();
  await expect(
    page.getByLabel("Nuova password locale", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Nuova password locale", { exact: true })
    .fill(password);
  await page
    .getByRole("button", { name: "Passa a password locale", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Cambia e revoca", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Passa a SSO", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Scollega", exact: true }).click();
  await page
    .getByRole("button", { name: "Scollega e revoca", exact: true })
    .click();
  await expect(
    page.getByText("Nessuna identità collegata.", { exact: false }),
  ).toBeVisible();
});

test("password rotation keeps this browser and revokes other sessions", async ({
  browser,
  page,
  context,
}) => {
  await login(page);
  await create(context, "password-browser");
  const other = await browser.newContext({ baseURL: origin });
  const second = await other.newPage();
  await currentLogout(page);
  await login(page, "password-browser");
  await login(second, "password-browser");
  await page.goto("/app/profile");
  await page.getByLabel("Password attuale").fill("wrong-password");
  await page
    .getByLabel("Nuova password", { exact: true })
    .fill("new-browser-password");
  await page
    .getByRole("button", { name: "Aggiorna password", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Aggiorna e revoca gli altri accessi",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "password attuale non è corretta",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Annulla", exact: true })
    .last()
    .click();
  await page.getByLabel("Password attuale").fill(password);
  await page
    .getByRole("button", { name: "Aggiorna password", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Aggiorna e revoca gli altri accessi",
      exact: true,
    })
    .click();
  await expect(
    page.getByText("Password aggiornata.", { exact: false }),
  ).toBeVisible();
  expect((await context.request.get("/api/v1/web/session")).status()).toBe(200);
  expect((await other.request.get("/api/v1/web/session")).status()).toBe(401);
  await second.reload();
  await expect(
    second.getByRole("heading", { name: "Accedi a FactoryVPN" }),
  ).toBeVisible();
  await other.close();
});

test("portal logout preserves another browser and Bearer access; global logout revokes both", async ({
  browser,
  page,
  context,
  request,
}) => {
  await login(page);
  const bearer = await (
    await completeApi(request, await request.post("/api/v1/auth/login", {
      data: { username: "root", password },
    }))
  ).json();
  const other = await browser.newContext({ baseURL: origin });
  const second = await other.newPage();
  await login(second);
  await currentLogout(page);
  expect((await other.request.get("/api/v1/web/session")).status()).toBe(200);
  expect(
    (
      await request.get("/api/v1/auth/me", {
        headers: { Authorization: "Bearer " + bearer.access_token },
      })
    ).status(),
  ).toBe(200);
  await login(page);
  await page.goto("/app/profile");
  await page
    .getByRole("button", { name: "Disconnetti tutti gli accessi", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Disconnetti tutto", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Accedi a FactoryVPN" }),
  ).toBeVisible();
  expect((await context.request.get("/api/v1/web/session")).status()).toBe(401);
  expect((await other.request.get("/api/v1/web/session")).status()).toBe(401);
  expect(
    (
      await request.get("/api/v1/auth/me", {
        headers: { Authorization: "Bearer " + bearer.access_token },
      })
    ).status(),
  ).toBe(401);
  await other.close();
});

test("OIDC browser code flow and independent SSO logout", async ({
  browser,
  page,
  context,
}) => {
  async function sso(p: Page) {
    await p.goto("/app/login");
    await p.getByRole("button", { name: "Provider di test" }).click();
    await p.getByRole("button", { name: "Continua come utente SSO" }).click();
    await expect(
      p.getByRole("heading", { name: "Accessi sotto controllo" }),
    ).toBeVisible();
    await expect(p).toHaveURL(origin + "/app/");
  }
  const other = await browser.newContext({
    baseURL: origin,
    ignoreHTTPSErrors: true,
  });
  const second = await other.newPage();
  await sso(page);
  await sso(second);
  expect((await context.request.get("/api/v1/web/session")).status()).toBe(200);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  expect(
    (await context.cookies()).filter((c) => c.name.includes("sso_")),
  ).toHaveLength(0);
  await currentLogout(page);
  await expect(page).toHaveURL(origin + "/app/login");
  expect((await other.request.get("/api/v1/web/session")).status()).toBe(200);
  await other.close();
});

test("mobile layout, keyboard dismissal, minimal permissions and audit", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, "technician");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await expect(
    page.getByRole("link", { name: "Permessi", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("link", { name: "Vai al contenuto", exact: true })
    .focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Apri menu", exact: true }),
  ).toBeFocused();
  await page.getByRole("button", { name: "Apri menu", exact: true }).click();
  await expect(
    page
      .getByRole("navigation")
      .getByRole("link", { name: "Utenti", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Permessi", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Permessi", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Connessione VPN", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".sidebar")).not.toHaveClass(/open/);
  await page.screenshot({
    path: "test-results/permissions-mobile.png",
    fullPage: true,
  });
  await page.goto("/app/users");
  await expect(
    page.getByRole("heading", { name: "Accesso non disponibile" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Apri menu", exact: true }).click();
  await page
    .getByRole("button", { name: "Esci dal portale", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await currentLogout(page);
  await login(page);
  await page.goto("/app/audit");
  await expect(
    page.getByRole("heading", { name: "Registro attività" }),
  ).toBeVisible();
  await page.getByLabel("Tipo di evento").fill("portal_login");
  await page.getByRole("button", { name: "Filtra eventi" }).click();
  await expect(
    page.getByText("portal_login", { exact: true }).first(),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("late mutation responses cannot repopulate another account’s cache", async ({
  page,
}) => {
  await login(page);
  let release!: () => void;
  let received!: () => void;
  const pending = new Promise<void>((resolve) => {
    received = resolve;
  });
  const resume = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/admin/users", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    received();
    await resume;
    await route.fulfill({ response });
  });
  await page.goto("/app/users/new");
  await page
    .getByLabel("Username", { exact: true })
    .fill("private-late-response");
  await page.getByLabel("Nome completo").fill("Informazione riservata Sud");
  await page.getByLabel("Password iniziale").fill(password);
  await page.getByLabel("Produzione Sud").check();
  await page.getByRole("button", { name: "Crea utente", exact: true }).click();
  await pending;
  await currentLogout(page);
  // Stay in this SPA so the previous mutation's callback is still alive.
  await page.getByLabel("Username", { exact: true }).fill("delegate");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await completeEmail(page, "delegate");
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith("/api/v1/admin/users") &&
      r.request().method() === "POST",
  );
  release();
  await response;
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  await expect(page).toHaveURL(origin + "/app/");
  await expect(
    page.getByText("Informazione riservata Sud", { exact: true }),
  ).toHaveCount(0);
});

test("session polling clears cached data and open forms after an external account change", async ({
  page,
  context,
}) => {
  await page.clock.install();
  await login(page);
  await page.goto("/app/users");
  await expect(
    page.getByRole("link", { name: "Apri southern", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Apri technician", exact: true })
    .click();
  await page.getByLabel("Nome completo").fill("Unsaved administrator data");

  // A callback or another window can replace the shared cookie without sending
  // this tab a BroadcastChannel notification. Keep the original SPA mounted.
  const changed = await completeApi(context.request, await context.request.post("/api/v1/web/login", {
    data: { username: "delegate", password },
    headers: { Origin: origin, "X-FactoryVPN-Portal": "1" },
  }), "delegate", true);
  expect(changed.status()).toBe(200);
  const refreshed = page.waitForResponse("**/api/v1/web/session");
  await page.clock.fastForward(31000);
  await refreshed;
  await expect(page.locator(".sidebar-profile")).toContainText("delegate");
  await expect(page.getByLabel("Nome completo")).not.toHaveValue(
    "Unsaved administrator data",
  );
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Utenti", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Apri technician", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Apri southern", exact: true }),
  ).toHaveCount(0);
});

test("an SSO callback in another tab clears the original tab's global group cache", async ({
  page,
  context,
}) => {
  await page.clock.install();
  await login(page);
  await page.goto("/app/groups");
  await expect(
    page.getByRole("heading", { name: "Produzione Sud", exact: true }),
  ).toBeVisible();
  const start = await call(context, "/web/sso/start", "POST", {
    provider_id: "test",
  });
  expect(start.status()).toBe(200);
  const second = await context.newPage();
  await second.goto((await start.json()).authorization_url);
  await second
    .getByRole("button", { name: "Continua come utente SSO" })
    .click();
  await expect(
    second.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
  const refreshed = page.waitForResponse("**/api/v1/web/session");
  await page.bringToFront();
  await page.clock.fastForward(31000);
  await refreshed;
  await expect(page.locator(".sidebar-profile")).toContainText("sso-user");
  await expect(
    page.getByRole("heading", { name: "Produzione Nord", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Produzione Sud", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Nuovo gruppo", exact: true }),
  ).toHaveCount(0);
  await second.close();
});
