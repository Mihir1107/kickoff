import { expect, test, type Page } from "@playwright/test";
import { allPages, settle } from "../support/pages";
import { CONTRAST_SRC } from "../support/pixels";

/**
 * Keyboard access (WCAG 2.1.1, 2.4.3, 2.4.7, 1.4.11 for the focus indicator):
 * every action is reachable by keyboard, Tab reaches every control with a visible focus ring of 3:1,
 * and the command palette and modals trap focus and give it back when they close.
 */
test.use({ reducedMotion: "reduce" });

const TABBABLE = `a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])`;

/** Identify an element across evaluations (no stable ids in the DOM). */
async function tagTabbables(page: Page) {
  return page.evaluate((sel) => {
    let n = 0;
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(sel)) {
      const r = el.getBoundingClientRect();
      if (el.matches(":disabled") || el.tabIndex < 0) continue; // not a Tab stop (disabled, or tabindex="-1")
      const visible = r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden" && !el.closest("[inert]") && !el.closest("[aria-hidden=true]");
      if (!visible) continue;
      el.dataset.kb = String(++n);
      out.push(`${el.dataset.kb}:${(el.getAttribute("aria-label") ?? el.textContent ?? el.tagName).trim().slice(0, 30)}`);
    }
    return out;
  }, TABBABLE);
}

test("every clickable element is reachable by keyboard", async ({ page }) => {
  test.setTimeout(300_000);
  const problems: string[] = [];
  for (const path of await allPages(page)) {
    await settle(page, path);
    const bad = await page.evaluate((sel) => {
      const out: string[] = [];
      for (const el of document.querySelectorAll<HTMLElement>("body *")) {
        const cs = getComputedStyle(el);
        if (cs.cursor !== "pointer" || cs.pointerEvents === "none") continue;
        if (el.closest("[aria-hidden=true], [inert]")) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        // Reachable: itself or an ancestor is tabbable (or a label whose control is), or it is a cell of a
        // keyboard-navigable composite widget (the reconciliation map).
        if (el.closest(sel) || el.closest("label") || el.closest('[role="group"][tabindex="0"]')) continue;
        out.push(`${el.tagName.toLowerCase()} "${(el.textContent ?? "").trim().slice(0, 30)}" .${String(el.className).slice(0, 60)}`);
      }
      return out;
    }, TABBABLE);
    problems.push(...bad.map((b) => `${path}: ${b}`));
  }
  expect(problems, problems.join("\n")).toEqual([]);
});

test("Tab reaches every control, each with a visible 3:1 focus ring", async ({ page }) => {
  test.setTimeout(900_000);
  const problems: string[] = [];
  for (const path of await allPages(page)) {
    await settle(page, path);
    const expected = await tagTabbables(page);
    // Walk the page with Tab, as a keyboard user would.
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    const reached = new Set<string>();
    for (let i = 0; i < expected.length + 5; i++) {
      await page.keyboard.press("Tab");
      const kb = await page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset.kb ?? null);
      if (kb) reached.add(kb);
    }
    const missed = expected.filter((e) => !reached.has(e.split(":")[0]!));
    problems.push(...missed.map((m) => `${path}: not reachable with Tab: ${m}`));

    // Ring: compare each control's surroundings focused vs unfocused, in rendered pixels.
    for (const id of expected.map((e) => e.split(":")[0]!)) {
      const el = page.locator(`[data-kb="${id}"]`);
      await el.evaluate((n) => (n as HTMLElement).scrollIntoView({ block: "center", inline: "nearest" }));
      // Reach it by keyboard (Shift+Tab then Tab lands back on it), so :focus-visible is the real one.
      await el.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      if (!(await el.evaluate((n) => n === document.activeElement))) continue; // first/last stop: covered by the Tab walk
      await page.waitForTimeout(30);
      // Measure where it is NOW (focusing can scroll), screenshot focused, then blur (never scrolls) for the baseline.
      const box = await el.boundingBox();
      if (!box || box.width < 2 || box.height < 2) continue;
      const vp = page.viewportSize()!;
      const pad = 6;
      const clip = { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad), width: Math.min(vp.width, box.x + box.width + pad) - Math.max(0, box.x - pad), height: Math.min(vp.height, box.y + box.height + pad) - Math.max(0, box.y - pad) };
      if (clip.width <= 2 * pad || clip.height <= 2 * pad) continue;
      const after = (await page.screenshot({ clip })).toString("base64");
      await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
      const before = (await page.screenshot({ clip })).toString("base64");
      const r = await page.evaluate(async ({ before, after, src, inset }) => {
        const { ratio } = new Function(`${src}; return { ratio };`)() as { ratio: (a: number[], b: number[]) => number };
        const load = async (b64: string) => {
          const img = new Image();
          img.src = `data:image/png;base64,${b64}`;
          await img.decode();
          const c = document.createElement("canvas");
          c.width = img.width; c.height = img.height;
          const g = c.getContext("2d", { willReadFrequently: true })!;
          g.drawImage(img, 0, 0);
          return { d: g.getImageData(0, 0, c.width, c.height).data, w: c.width, h: c.height };
        };
        const A = await load(before), B = await load(after);
        const at = (I: typeof A, x: number, y: number) => { const i = (Math.round(y) * I.w + Math.round(x)) * 4; return [I.d[i]!, I.d[i + 1]!, I.d[i + 2]!]; };
        // The ring sits 2..4px outside the box (outline-offset 2, width 2): sample the middle of each side.
        const rs: number[] = [];
        for (let t = 0.3; t <= 0.7; t += 0.1) {
          for (const d of [2.5, 3.5]) {
            const pts = [[inset.l + (A.w - inset.l - inset.r) * t, inset.t - d], [inset.l + (A.w - inset.l - inset.r) * t, A.h - inset.b + d - 1], [inset.l - d, inset.t + (A.h - inset.t - inset.b) * t], [A.w - inset.r + d - 1, inset.t + (A.h - inset.t - inset.b) * t]];
            for (const [x, y] of pts) if (x! >= 0 && y! >= 0 && x! < A.w && y! < A.h) rs.push(ratio(at(A, x!, y!), at(B, x!, y!)));
          }
        }
        rs.sort((a, b) => a - b);
        return rs.length ? rs[Math.floor(rs.length / 2)]! : 0;
      }, { before, after, src: CONTRAST_SRC, inset: { l: box.x - clip.x, t: box.y - clip.y, r: clip.x + clip.width - (box.x + box.width), b: clip.y + clip.height - (box.y + box.height) } });
      const label = expected.find((e) => e.startsWith(`${id}:`))!;
      if (r < 3) problems.push(`${path}: focus ring ${r.toFixed(2)}:1 < 3:1 on ${label}`);
    }
  }
  expect(problems, problems.join("\n")).toEqual([]);
});

test("command palette: opens from the keyboard, traps focus, restores it on close", async ({ page }) => {
  await settle(page, "/");
  const jump = page.getByRole("button", { name: /Jump to/ });
  await jump.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Command palette" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("combobox", { name: /Search/ })).toBeFocused();
  // With results listed (options are tabindex=-1 buttons) Tab must still stay on the search box.
  await expect(page.getByRole("option").first()).toBeAttached();
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press(i % 2 ? "Shift+Tab" : "Tab");
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  }
  await expect(page.locator("#root")).toHaveJSProperty("inert", true);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(jump).toBeFocused();

  // The shortcut from anywhere, then arrows + Enter navigate.
  await page.keyboard.press("ControlOrMeta+k");
  await expect(dialog).toBeVisible();
  await page.keyboard.type("Clients");
  await expect(page.getByRole("option", { selected: true })).toContainText("Clients");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/clients$/);
  await expect(page.locator("#root")).toHaveJSProperty("inert", false);
});

for (const [name, open] of [
  ["new client modal", async (page: Page) => { await settle(page, "/clients"); return page.getByRole("button", { name: "New client" }); }],
  ["connect source modal", async (page: Page) => {
    await settle(page, "/clients");
    await page.locator('a[href^="/clients/"]').first().click();
    await page.getByRole("button", { name: "Connections" }).click();
    return page.getByRole("button", { name: "Connect source" });
  }],
] as const) {
  test(`${name}: traps focus both ways, Escape closes, focus returns to the opener`, async ({ page }) => {
    const opener = await open(page);
    await opener.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect.poll(() => dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    for (const key of ["Tab", "Tab", "Tab", "Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"]) {
      await page.keyboard.press(key);
      expect(await dialog.evaluate((d) => d.contains(document.activeElement)), `focus escaped after ${key}`).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(opener).toBeFocused();
  });
}

test("reconciliation map: one tab stop, arrow keys read each unit", async ({ page }) => {
  await settle(page, "/collections");
  await page.locator('a[href^="/jobs/"]').first().click();
  const map = page.getByRole("group", { name: /Reconciliation map/ });
  await map.focus();
  const first = await page.locator("[data-unit]").first().getAttribute("data-unit");
  await expect(page.getByTestId("unit-tooltip")).toContainText(first!);
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  const third = await page.locator("[data-active]").getAttribute("data-unit");
  expect(third).not.toBe(first);
  await expect(page.getByTestId("unit-tooltip")).toContainText(third!);
  await expect(page.locator('[aria-live="polite"]').filter({ hasText: "expected" })).toContainText(third!.split("/")[1]!);
  await page.keyboard.press("Tab");
  await expect(map).not.toBeFocused(); // a single tab stop: Tab leaves the map
});

test("export drop zone opens the file chooser from the keyboard", async ({ page }) => {
  await settle(page, "/exports");
  const zone = page.getByRole("button", { name: /Choose a workspace export ZIP/ });
  await zone.focus();
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.keyboard.press("Enter")]);
  expect(chooser.isMultiple()).toBe(false);
});
