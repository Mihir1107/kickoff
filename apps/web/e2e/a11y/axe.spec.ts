import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { allPages, settle } from "../support/pages";

/**
 * axe-core on every page and in the overlay states (command palette, modals): zero serious or
 * critical violations. WCAG 2.0/2.1/2.2 A and AA rules plus axe best practices.
 */
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function violations(page: Page, where: string): Promise<string[]> {
  const result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return result.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .flatMap((v) => v.nodes.map((n) => `${where} [${v.impact}] ${v.id}: ${n.target.join(" ")} — ${n.failureSummary?.split("\n").slice(1).join(" ").trim() ?? v.help}`));
}

test("no serious or critical axe violations on any page", async ({ page }) => {
  test.setTimeout(300_000);
  const found: string[] = [];
  for (const path of await allPages(page)) {
    await settle(page, path);
    found.push(...(await violations(page, path)));
  }
  expect(found, found.join("\n")).toEqual([]);
});

test("no serious or critical axe violations with overlays open", async ({ page }) => {
  const found: string[] = [];
  await settle(page, "/");
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  found.push(...(await violations(page, "command palette")));
  await page.keyboard.press("Escape");

  await settle(page, "/clients");
  await page.getByRole("button", { name: "New client" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.waitForTimeout(500);
  found.push(...(await violations(page, "new client modal")));
  await page.keyboard.press("Escape");

  await page.locator('a[href^="/clients/"]').first().click();
  await page.getByRole("button", { name: "Connections" }).click();
  await page.getByRole("button", { name: "Connect source" }).click();
  await page.getByRole("button", { name: /Slack internal app/ }).click();
  await page.waitForTimeout(500);
  found.push(...(await violations(page, "connect source modal")));
  expect(found, found.join("\n")).toEqual([]);
});
