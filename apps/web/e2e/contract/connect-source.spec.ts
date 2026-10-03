import { expect, test } from "@playwright/test";
import { CLIENT_ID, stubApi } from "../support/stub-api";

/** An opaque canary: if it shows up anywhere but the one POST body, the token leaked. */
const CANARY = "xoxb-CANARY-7f3e91-do-not-leak";

test("internal-app token: sent once in the POST body, with CSRF, and nowhere else", async ({ page }) => {
  const seen = await stubApi(page);
  const logs: string[] = [];
  page.on("console", (m) => logs.push(m.text()));

  await page.goto(`/clients/${CLIENT_ID}?tab=connections`);
  await page.getByRole("button", { name: "Connect source" }).click();
  await page.getByRole("button", { name: /Slack internal app/ }).click();
  await expect(page.getByLabel("Slack workspace id")).toHaveCount(0); // the team id comes from auth.test, server-side
  await page.getByLabel("Bot token").fill(CANARY);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("Connection created")).toBeVisible();

  const posts = seen.filter((r) => r.method === "POST" && r.url.endsWith(`/clients/${CLIENT_ID}/connections/slack/token`));
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0]!.body!)).toEqual({ token: CANARY });
  expect(posts[0]!.headers["x-csrf-token"]).toBe("csrf-bound-to-session");

  const elsewhere = seen.filter((r) => r !== posts[0] && (r.url.includes(CANARY) || (r.body ?? "").includes(CANARY) || JSON.stringify(r.headers).includes(CANARY)));
  expect(elsewhere).toEqual([]);
  expect(seen.some((r) => r.headers.authorization)).toBe(false); // sessions only: never a bearer token
  expect(page.url()).not.toContain(CANARY);
  expect(await page.content()).not.toContain(CANARY); // field cleared, modal closed, not echoed
  expect(await page.evaluate((c) => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(c), CANARY)).toBe(false);
  expect(logs.filter((l) => l.includes(CANARY))).toEqual([]);
  await expect(page.getByLabel("Bot token")).toHaveCount(0);
});

test("a rejected token is not echoed, and the field never posts natively", async ({ page }) => {
  await stubApi(page, {
    [`POST /v1/clients/${CLIENT_ID}/connections/slack/token`]: () => ({ status: 422, body: { error: "unprocessable", detail: "token rejected by Slack" } }),
  });
  await page.goto(`/clients/${CLIENT_ID}?tab=connections`);
  await page.getByRole("button", { name: "Connect source" }).click();
  await page.getByRole("button", { name: /Slack internal app/ }).click();
  const token = page.getByLabel("Bot token");
  expect(await token.getAttribute("name")).toBeNull(); // nothing for a native submit to serialize
  expect(await token.getAttribute("type")).toBe("password");
  await token.fill(CANARY);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("token rejected by Slack")).toBeVisible();
  const visibleText = await page.locator("body").innerText();
  expect(visibleText).not.toContain(CANARY);
});

test("OAuth sources: POST to start the flow (with CSRF), then only follow the provider URL", async ({ page }) => {
  const seen = await stubApi(page);
  await page.goto(`/clients/${CLIENT_ID}?tab=connections`);
  await page.getByRole("button", { name: "Connect source" }).click();
  for (const [option, provider] of [[/Install our app/, "Slack"], [/admin consent/, "Microsoft"]] as const) {
    await page.getByRole("button", { name: option }).click();
    await expect(page.getByLabel("Bot token")).toHaveCount(0);
    await expect(page.getByRole("button", { name: `Continue to ${provider}` })).toBeVisible();
  }
  await Promise.all([
    page.waitForURL(/^https:\/\/login\.microsoftonline\.com\/common\/adminconsent/),
    page.getByRole("button", { name: "Continue to Microsoft" }).click(),
  ]);
  const mutations = seen.filter((r) => r.method !== "GET");
  expect(mutations.map((r) => `${r.method} ${new URL(r.url).pathname}`)).toEqual([`POST /v1/clients/${CLIENT_ID}/connections/teams/consent`]);
  expect(mutations[0]!.headers["x-csrf-token"]).toBe("csrf-bound-to-session");
  expect(JSON.parse(mutations[0]!.body!)).toEqual({});
});

test("an install URL that is not https is never followed", async ({ page }) => {
  await stubApi(page, {
    [`POST /v1/clients/${CLIENT_ID}/connections/slack/install`]: () => ({ status: 200, body: { authorize_url: "javascript:alert(document.cookie)" } }),
  });
  await page.goto(`/clients/${CLIENT_ID}?tab=connections`);
  await page.getByRole("button", { name: "Connect source" }).click();
  await page.getByRole("button", { name: /Install our app/ }).click();
  await page.getByRole("button", { name: "Continue to Slack" }).click();
  await expect(page.getByText("Unexpected install URL from the API")).toBeVisible();
  expect(new URL(page.url()).pathname).toBe(`/clients/${CLIENT_ID}`);
});

test("401 reauth_required sends the user back through the IdP", async ({ page }) => {
  await stubApi(page, {
    [`POST /v1/clients/${CLIENT_ID}/close`]: () => ({ status: 401, body: { error: "reauth_required", detail: "recent sign-in required" } }),
  });
  await page.goto(`/clients/${CLIENT_ID}`);
  const [login] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/v1/auth/login")),
    page.getByRole("button", { name: "Close client" }).click(),
  ]);
  const url = new URL(login.url());
  expect(url.searchParams.get("reauth")).toBe("1");
  expect(url.searchParams.get("return_to")).toBe(`/clients/${CLIENT_ID}`);
});

test("re-authorizing: only that source's methods; a new internal-app token replaces via PUT", async ({ page }) => {
  const seen = await stubApi(page);
  await page.goto(`/clients/${CLIENT_ID}?tab=connections`);
  await page.getByRole("button", { name: "Re-authorize" }).click();
  expect(await page.locator("[aria-pressed] .font-medium").allInnerTexts()).toEqual(["Slack", "Slack internal app"]);
  await page.getByRole("button", { name: /Slack internal app/ }).click();
  await page.getByLabel("Bot token").fill(CANARY);
  await page.getByRole("button", { name: "Replace token" }).click();
  await expect(page.getByText("Connection re-authorized")).toBeVisible();
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts.map((r) => new URL(r.url).pathname)).toEqual(["/v1/connections/01990000-0000-7000-8000-0000000000c1/token"]);
  expect(JSON.parse(puts[0]!.body!)).toEqual({ token: CANARY });
  expect(puts[0]!.headers["x-csrf-token"]).toBe("csrf-bound-to-session");
  expect(await page.content()).not.toContain(CANARY);
});
