import { expect, test } from "@playwright/test";
import { CLIENT_ID, stubApi } from "../support/stub-api";

/** ADR 0016: errors come back by redirect as ?auth_error= / ?install_error= codes; the UI explains them. */

test("a failed sign-in lands on / without a session: sign-in page, clear message, code gone from the URL", async ({ page }) => {
  await stubApi(page, { "GET /v1/me": () => ({ status: 401, body: { error: "unauthenticated", detail: "" } }) });
  await page.goto("/?auth_error=unknown_user");
  await expect(page).toHaveURL(/\/login/);
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Sign-in failed");
  await expect(alert).toContainText("not registered for this workspace");
  expect(page.url()).not.toContain("auth_error");
});

const AUTH: Record<string, RegExp> = {
  denied: /cancelled or refused/,
  expired: /expired/,
  invalid: /could not be verified/,
  unknown_user: /not registered/,
  inactive: /deactivated/,
};
for (const [code, message] of Object.entries(AUTH)) {
  test(`auth_error=${code} has its own message`, async ({ page }) => {
    await stubApi(page);
    await page.goto(`/?auth_error=${code}`);
    await expect(page.getByRole("alert")).toContainText(message);
    await expect(page.getByRole("alert")).not.toContainText("Code:");
    await expect(page).not.toHaveURL(/auth_error/);
  });
}

test("unknown codes get a generic message plus the code; junk is never echoed", async ({ page }) => {
  await stubApi(page);
  await page.goto(`/?auth_error=idp_on_fire`);
  await expect(page.getByRole("alert")).toContainText("Sign-in did not complete.");
  await expect(page.getByRole("alert")).toContainText("Code: idp_on_fire");
  await page.getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);

  for (const junk of ["constructor", "<img src=x onerror=alert(1)>", "a".repeat(80)]) {
    await page.goto(`/?auth_error=${encodeURIComponent(junk)}`);
    const alert = page.getByRole("alert");
    await expect(alert).toContainText("Sign-in did not complete.");
    if (junk === "constructor") await expect(alert).toContainText("Code: constructor"); // not a prototype lookup
    else {
      await expect(alert).toContainText("Code: unrecognised");
      await expect(alert).not.toContainText(junk.slice(0, 20));
    }
  }
});

test("install_error on the connection page: generic message plus the code (the ADR names no codes yet)", async ({ page }) => {
  await stubApi(page);
  await page.goto(`/clients/${CLIENT_ID}?tab=connections&install_error=access_denied`);
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Connection failed");
  await expect(alert).toContainText("could not be connected. Nothing was changed.");
  await expect(alert).toContainText("Code: access_denied");
  await expect(page).toHaveURL(new RegExp(`/clients/${CLIENT_ID}\\?tab=connections$`));
});
