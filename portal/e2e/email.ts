import { expect, type APIRequestContext, type APIResponse, type Page } from "@playwright/test";
export async function codeFor(request: APIRequestContext, username: string) {
  const recipient = username.includes("@") ? username : username + "@example.test";
  const response = await request.get(process.env.FACTORYVPN_MAIL_URL + "/messages/" + encodeURIComponent(recipient));
  expect(response.ok()).toBeTruthy();
  return (await response.json()).code as string;
}
export async function completeEmail(page: Page, username: string, force = false) {
  if (!force && !["root", "delegate", "gateway-editor"].includes(username)) return;
  await expect(page.getByLabel("Codice email", { exact: true })).toBeVisible();
  await page.getByLabel("Codice email", { exact: true }).fill(await codeFor(page.request, username));
  await page.getByRole("button", { name: "Verifica codice", exact: true }).click();
}
export async function completeApi(request: APIRequestContext, response: APIResponse, username = "root", portal = false) {
  if (response.status() !== 200 || !(await response.json()).challenge_required) return response;
  return request.post("/api/v1/" + (portal ? "web" : "auth") + "/login/verify", {
    data: { challenge_token: (await response.json()).challenge_token, code: await codeFor(request, username) },
    headers: { Origin: process.env.FACTORYVPN_PORTAL_URL!, "X-FactoryVPN-Portal": "1" },
  });
}
