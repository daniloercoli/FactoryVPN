import { execFileSync } from "node:child_process";
import { test, expect, type APIRequestContext } from "./fixtures";

const password = "factoryvpn-browser-tests";

function issueCode(username: string, purpose: "login" | "confirm" = "login") {
  const output = execFileSync(
    "../.venv/bin/python",
    [
      "-m",
      "scripts.issue_console_code",
      username,
      "--operator",
      "browser-test",
      "--reason",
      "SMTP outage test",
      "--purpose",
      purpose,
    ],
    {
      input: "EMETTI\n",
      encoding: "utf8",
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 30000,
    },
  );
  const match = output.match(
    /Console code \((?:login|confirm)\): ([0-9a-f]{6}(?:-[0-9a-f]{6}){3})/i,
  );
  if (!match) throw new Error("Console did not issue the expected code");
  return match[1];
}

async function createAccount(
  request: APIRequestContext,
  username: string,
  admin = false,
) {
  const login = await request.post("/api/v1/auth/login", {
    data: { username: "root", password, emergency_code: issueCode("root") },
  });
  expect(login.status()).toBe(200);
  const created = await request.post("/api/v1/admin/users", {
    headers: { Authorization: "Bearer " + (await login.json()).access_token },
    data: {
      username,
      password,
      email: username + "@example.test",
      is_admin: admin,
      permissions: admin ? [] : ["connect", "gateways.manage"],
      group_ids: admin ? [] : ["00000000-0000-4000-8000-000000000001"],
    },
  });
  expect(created.status()).toBe(201);
}

test("technician can choose console access after a mail error and confirm identity without email", async ({
  page,
  request,
  context,
}) => {
  const username = "console-technician";
  await createAccount(request, username);
  const code = issueCode(username);
  let normalAttempts = 0;
  let emergencyAttempts = 0;
  await page.route("**/api/v1/web/login", async (route) => {
    if (!route.request().postDataJSON().emergency_code) {
      normalAttempts += 1;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Email delivery unavailable; access has not been granted",
        }),
      });
    } else {
      emergencyAttempts += 1;
      await route.continue();
    }
  });
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Invio email non disponibile",
  );
  expect(emergencyAttempts).toBe(0);

  await page
    .getByRole("checkbox", { name: "Usa codice di emergenza", exact: true })
    .check();
  await page.getByLabel("Password", { exact: true }).fill(password);
  const wrong = (code[0] === "0" ? "1" : "0") + code.slice(1);
  await page.getByLabel("Codice di emergenza", { exact: true }).fill(wrong);
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Codice di emergenza errato",
  );
  await expect(
    page.getByLabel("Codice di emergenza", { exact: true }),
  ).toHaveValue("");

  await page.getByLabel("Password", { exact: true }).fill(password);
  await page
    .getByLabel("Codice di emergenza", { exact: true })
    .fill(code.toUpperCase());
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();
  expect(normalAttempts).toBe(1);
  expect(emergencyAttempts).toBe(2);
  const sessionResponse = await context.request.get("/api/v1/web/session");
  expect(sessionResponse.status()).toBe(200);
  const session = await sessionResponse.json();
  expect(session.user.is_admin).toBe(false);
  expect(session.user.email_verified).toBe(false);
  expect(Date.parse(session.expires_at) - Date.now()).toBeGreaterThan(
    11 * 60 * 60 * 1000,
  );

  await page.goto("/app/profile");
  await page
    .getByRole("combobox", { name: "Operazione", exact: true })
    .selectOption("confirm");
  await page
    .getByRole("checkbox", { name: "Usa codice di emergenza", exact: true })
    .check();
  await page.getByLabel("Password di conferma", { exact: true }).fill(password);
  await page
    .getByLabel("Codice di emergenza", { exact: true })
    .fill(issueCode(username, "confirm"));
  const confirmed = page.waitForResponse(
    (response) =>
      response.url().endsWith("/web/email/confirm") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Conferma identità", exact: true })
    .click();
  expect((await confirmed).status()).toBe(200);
  await expect(
    page.getByText(
      "Identità confermata. Puoi proseguire con le operazioni protette per cinque minuti.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByLabel("Codice email", { exact: true })).toHaveCount(0);
  await expect(
    page.getByLabel("Codice di emergenza", { exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByLabel("Password di conferma", { exact: true }),
  ).toHaveValue("");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/console-profile-mobile.png",
    fullPage: true,
  });
});

test("administrator can confirm backup operations with a new console code", async ({
  page,
  request,
}) => {
  const username = "console-backup-admin";
  await createAccount(request, username, true);
  await page.goto("/app/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page
    .getByRole("checkbox", { name: "Usa codice di emergenza", exact: true })
    .check();
  await page
    .getByLabel("Codice di emergenza", { exact: true })
    .fill(issueCode(username));
  await page.getByRole("button", { name: "Accedi", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Accessi sotto controllo" }),
  ).toBeVisible();

  let requireConfirmation = true;
  await page.route("**/api/v1/backups", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      response,
      json: {
        ...body,
        needs_reauthentication:
          requireConfirmation || body.needs_reauthentication,
      },
    });
  });
  await page.route("**/api/v1/backups/reauthenticate", async (route) => {
    const response = await route.fetch();
    if (response.ok()) requireConfirmation = false;
    await route.fulfill({ response });
  });
  await page.goto("/app/backups");
  const form = page.getByRole("region", { name: "Conferma identità" });
  await form.getByLabel("Password attuale", { exact: true }).fill(password);
  await form
    .getByRole("checkbox", { name: "Usa codice di emergenza", exact: true })
    .check();
  await form
    .getByLabel("Codice di emergenza", { exact: true })
    .fill(issueCode(username, "confirm"));
  await form
    .getByRole("button", { name: "Conferma identità", exact: true })
    .click();
  await expect(
    page.getByText("Coordinatore disponibile", { exact: true }),
  ).toBeVisible();
  await expect(form).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Crea backup", exact: true }),
  ).toBeEnabled();
  await expect(page.getByLabel("Codice email", { exact: true })).toHaveCount(0);
});

for (const admin of [false, true]) {
  for (const expired of [false, true]) {
    const role = admin ? "admin" : "technician";
    const challengeState = expired ? "expired" : "waiting";
    test(`${role} can choose console access after an accepted but undelivered ${challengeState} email`, async ({
      page,
      request,
      context,
    }) => {
      const username = `console-mail-${role}-${challengeState}`;
      await createAccount(request, username, admin);
      if (!admin) await page.setViewportSize({ width: 390, height: 844 });

      let loginRequests = 0;
      let emergencyRequests = 0;
      page.on("request", (request) => {
        if (new URL(request.url()).pathname.startsWith("/api/v1/web/login"))
          loginRequests += 1;
      });
      await page.route("**/api/v1/web/login", async (route) => {
        if (route.request().postDataJSON().emergency_code) {
          emergencyRequests += 1;
          await route.continue();
          return;
        }
        // The real SMTP sink accepts the message. Deliberately never retrieve
        // its email code: provider acceptance does not establish delivery.
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        const challenge = await response.json();
        expect(challenge.challenge_required).toBe(true);
        await route.fulfill({
          response,
          json: {
            ...challenge,
            ...(expired
              ? { expires_at: new Date(Date.now() - 1000).toISOString() }
              : {}),
          },
        });
      });

      await page.goto("/app/login");
      await page.getByLabel("Username", { exact: true }).fill(username);
      await page.getByLabel("Password", { exact: true }).fill(password);
      await page.getByRole("button", { name: "Accedi", exact: true }).click();
      await expect(
        page.getByLabel("Codice email", { exact: true }),
      ).toBeVisible();
      const emergency = page.getByRole("button", {
        name: "Non ricevi l’email? Usa un codice di emergenza",
        exact: true,
      });
      await expect(emergency).toBeVisible();
      await expect(emergency).toBeEnabled();
      if (expired) {
        await expect(page.getByRole("alert")).toContainText("Codice scaduto");
        await expect(
          page.getByLabel("Codice email", { exact: true }),
        ).toBeDisabled();
      } else {
        await expect(
          page.getByRole("button", { name: /Invia di nuovo tra/ }),
        ).toBeDisabled();
      }
      expect((await context.request.get("/api/v1/web/session")).status()).toBe(
        401,
      );
      expect(loginRequests).toBe(1);
      expect(emergencyRequests).toBe(0);
      await page.screenshot({
        path: `test-results/console-email-${role}-${challengeState}.png`,
        fullPage: true,
      });

      await emergency.click();
      await expect(page.getByLabel("Username", { exact: true })).toHaveValue(
        username,
      );
      await expect(page.getByLabel("Password", { exact: true })).toHaveValue(
        "",
      );
      await expect(
        page.getByRole("checkbox", {
          name: "Usa codice di emergenza",
          exact: true,
        }),
      ).toBeChecked();
      await expect(
        page.getByLabel("Codice di emergenza", { exact: true }),
      ).toHaveValue("");
      await expect(
        page.getByLabel("Codice email", { exact: true }),
      ).toHaveCount(0);
      expect(loginRequests).toBe(1);
      expect(emergencyRequests).toBe(0);
      expect((await context.request.get("/api/v1/web/session")).status()).toBe(
        401,
      );

      await page.getByLabel("Password", { exact: true }).fill(password);
      await page
        .getByLabel("Codice di emergenza", { exact: true })
        .fill(issueCode(username));
      await page.getByRole("button", { name: "Accedi", exact: true }).click();
      await expect(
        page.getByRole("heading", { name: "Accessi sotto controllo" }),
      ).toBeVisible();
      expect(loginRequests).toBe(2);
      expect(emergencyRequests).toBe(1);
      const session = await context.request.get("/api/v1/web/session");
      expect(session.status()).toBe(200);
      expect((await session.json()).user.is_admin).toBe(admin);
    });
  }
}
