import { expect, test } from "@playwright/test";

/** prefers-reduced-motion: the effects named in the review are gone, and they still work without it. */
test.describe("reduced motion", () => {
  test.use({ reducedMotion: "reduce" });

  test("no hash rain, no spotlight, no rolling numbers, no verify sweep", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator('[class*="writing-mode"]')).toHaveCount(0);

    await page.goto("/");
    const counter = page.locator(".text-\\[40px\\]").nth(2);
    await expect(counter).not.toBeEmpty({ timeout: 15_000 });
    const values = new Set<string>();
    for (let i = 0; i < 20; i++) {
      values.add((await counter.textContent()) ?? "");
      await page.waitForTimeout(50);
    }
    expect([...values]).toHaveLength(1); // the final value at once, never intermediate frames
    await expect(page.locator(".glass > .pointer-events-none.absolute.inset-0")).toHaveCount(0);

    await page.goto("/collections");
    await page.locator('a[href^="/jobs/"]').first().click();
    await page.getByRole("button", { name: /Verify chain/ }).click();
    await page.waitForTimeout(700); // mid-request: an animated sweep would have ticked blocks by now
    await expect(page.locator("[data-seq] .bg-mint")).toHaveCount(0);
    await expect(page.getByText("Chain intact")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("animate")).toHaveCount(0);
  });
});

test("without the preference the effects run (the gates are not stuck on)", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".glass > .pointer-events-none.absolute.inset-0").first()).toBeAttached({ timeout: 15_000 });
  await page.goto("/collections");
  await page.locator('a[href^="/jobs/"]').first().click();
  await page.getByRole("button", { name: /Verify chain/ }).click();
  await page.waitForTimeout(700);
  expect(await page.locator("[data-seq] .bg-mint").count()).toBeGreaterThan(0);
});
