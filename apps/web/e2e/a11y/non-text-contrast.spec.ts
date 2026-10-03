import { expect, test, type Page } from "@playwright/test";
import { allPages, settle } from "../support/pages";
import { capture, CONTRAST_SRC, HIDE_TEXT } from "../support/pixels";

/**
 * WCAG 2.1 SC 1.4.11 Non-text Contrast (3:1), measured on rendered pixels:
 * - inputs and selects: their boundary (border or fill) against the surface around them;
 * - outline/danger buttons, selected choice cards and the active tab: the border that marks them;
 * - heatmap cells and reconciliation bar segments: their fill against the gap/panel around them;
 * - progress bars: the fill against its track; progress rings: the arc against its track and the panel.
 * Focus indicators are measured in keyboard.spec.ts (focused vs unfocused pixels).
 * Runs with reduced motion so frames are deterministic; decorative graphics (aria-hidden) are excluded.
 */
test.use({ reducedMotion: "reduce" });

/** What the audit measures, as a string: if it differs before and after a screenshot, the frame was torn. */
const signature = (page: Page) =>
  page.evaluate(() => {
    let sig = "";
    for (const el of document.querySelectorAll<HTMLElement>("[data-unit], [data-segment], [data-progress-fill], svg[data-ring], [data-variant], [aria-pressed], input, select")) {
      const r = el.getBoundingClientRect();
      sig += `${el.dataset.unitState ?? ""}|${el.getAttribute("data-value") ?? ""}|${el.style.opacity}|${Math.round(r.x)},${Math.round(r.y)},${Math.round(r.width)};`;
    }
    return sig;
  });

async function audit(page: Page): Promise<string[]> {
  await page.addStyleTag({ content: HIDE_TEXT });
  await page.waitForTimeout(200);
  // Live pages (a running job) keep changing: retake the frame until the measured graphics are the same
  // before and after the screenshot, so pixels and geometry describe the same moment.
  for (let attempt = 0; ; attempt++) {
    const before = await signature(page);
    await capture(page);
    if ((await signature(page)) === before || attempt === 5) break;
  }
  return page.evaluate((src) => {
    const { ratio } = new Function(`${src}; return { ratio };`)() as { ratio: (a: number[], b: number[]) => number };
    const px = (window as unknown as { __px: (x: number, y: number) => number[] }).__px;
    const W = innerWidth, H = innerHeight;
    const inView = (r: DOMRect) => r.width > 2 && r.height > 2 && r.top >= 0 && r.left >= 0 && r.bottom <= H && r.right <= W;
    const median = (pts: number[][]) => [0, 1, 2].map((i) => pts.map((p) => p[i]!).sort((a, b) => a - b)[Math.floor(pts.length / 2)]!);
    const label = (el: Element) => (el.getAttribute("aria-label") ?? el.getAttribute("data-unit") ?? el.getAttribute("data-segment") ?? el.textContent ?? el.tagName).trim().slice(0, 30);
    const fails: string[] = [];
    // With a modal open only the dialog is operable; what is dimmed behind its backdrop is not audited.
    const root: ParentNode = document.querySelector('[role="dialog"][aria-modal="true"]') ?? document;
    const check = (what: string, el: Element, r: number) => { if (r < 3) fails.push(`${what} ${r.toFixed(2)}:1 < 3:1 "${label(el)}"`); };

    /** Points along the middle half of each edge: `d` px inside (positive) or outside (negative) the box. */
    const edge = (b: DOMRect, d: number) => {
      const pts: number[][] = [];
      for (let t = 0.3; t <= 0.7; t += 0.1) {
        pts.push(px(b.left + b.width * t, b.top + d), px(b.left + b.width * t, b.bottom - 1 - d));
        if (b.height > 12) pts.push(px(b.left + d, b.top + b.height * t), px(b.right - 1 - d, b.top + b.height * t));
      }
      return median(pts);
    };
    /**
     * Contrast of a box's boundary against what is just outside it. Edges sit at sub-pixel positions and
     * are anti-aliased, so each sample point looks across a 0..band px strip inside the edge and keeps its
     * strongest pixel against that point's own outside pixel; the result is the median over the points.
     */
    const boundary = (b: DOMRect, band: number) => {
      const rs: number[] = [];
      const sample = (inside: (d: number) => number[], outside: number[]) => {
        let best = 1;
        for (let d = 0; d <= band; d += 0.5) best = Math.max(best, ratio(inside(d), outside));
        rs.push(best);
      };
      for (let t = 0.3; t <= 0.7; t += 0.1) {
        const x = b.left + b.width * t, y = b.top + b.height * t;
        sample((d) => px(x, Math.floor(b.top + d)), px(x, b.top - 3));
        sample((d) => px(x, Math.ceil(b.bottom - 1 - d)), px(x, b.bottom + 2));
        if (b.height > 12) {
          sample((d) => px(Math.floor(b.left + d), y), px(b.left - 3, y));
          sample((d) => px(Math.ceil(b.right - 1 - d), y), px(b.right + 2, y));
        }
      }
      return rs.sort((a, c) => a - c)[Math.floor(rs.length / 2)]!;
    };
    const centre = (b: DOMRect) => median([[0.5, 0.5], [0.35, 0.5], [0.65, 0.5], [0.5, 0.35], [0.5, 0.65]].map(([x, y]) => px(b.left + b.width * x!, b.top + b.height * y!)));

    // Inputs: the boundary that identifies the field (its border, or a fill distinct from the surface).
    for (const field of root.querySelectorAll("input:not([type=hidden]):not([type=file]), select, textarea")) {
      const el = field.closest("[data-field]") ?? field; // a wrapper that draws the field's boundary
      const b = el.getBoundingClientRect();
      if (!inView(b) || el.closest("[aria-hidden=true]")) continue;
      const bw = parseFloat(getComputedStyle(el).borderTopWidth) || 0;
      check("input boundary", el, Math.max(boundary(b, bw + 1), ratio(centre(b), edge(b, -3))));
    }
    // Bordered buttons, selected choice cards, active tabs: the border is what marks them.
    for (const el of root.querySelectorAll('[data-variant="outline"], [data-variant="danger"], [aria-pressed="true"]')) {
      const b = el.getBoundingClientRect();
      if (!inView(b) || el.closest("[aria-hidden=true]") || (el as HTMLButtonElement).disabled) continue;
      const bw = parseFloat(getComputedStyle(el).borderTopWidth) || 0;
      check(el.matches("[aria-pressed]") ? "selected-state border" : "button border", el, boundary(b, bw + 1));
    }
    // Heatmap cells and bar segments: fill against what surrounds them.
    for (const el of root.querySelectorAll("[data-unit], [data-segment]")) {
      const b = el.getBoundingClientRect();
      if (!inView(b)) continue;
      const out = el.matches("[data-segment]") ? median([px(b.left + b.width / 2, b.top - 4), px(b.left + b.width / 2, b.bottom + 3)]) : median([px(b.left - 2, b.top + b.height / 2), px(b.right + 1, b.top + b.height / 2), px(b.left + b.width / 2, b.top - 2), px(b.left + b.width / 2, b.bottom + 1)]);
      // A hollow cell is identified by its outline, a filled one by its fill: take whichever marks it.
      check(el.matches("[data-unit]") ? `heatmap cell (${el.getAttribute("data-unit-state")})` : "bar segment", el, Math.max(ratio(centre(b), out), boundary(b, 2)));
    }
    // Progress bars: fill against the empty track.
    for (const track of root.querySelectorAll("[data-progress-track]")) {
      const fill = track.querySelector("[data-progress-fill]");
      const tb = track.getBoundingClientRect(), fb = fill?.getBoundingClientRect();
      if (!fill || !fb || !inView(tb) || fb.width < 4 || tb.right - fb.right < 4) continue;
      check("progress fill vs track", fill, ratio(px(fb.left + fb.width / 2, fb.top + fb.height / 2), px((fb.right + tb.right) / 2, tb.top + tb.height / 2)));
    }
    // Rings: the arc against its track and against the panel just outside the ring.
    for (const svg of root.querySelectorAll("svg[data-ring]")) {
      const b = svg.getBoundingClientRect();
      const v = Number(svg.getAttribute("data-value")), r = Number(svg.getAttribute("data-r")), s = Number(svg.getAttribute("data-stroke"));
      if (!inView(b) || v < 0.05) continue;
      const cx = b.left + b.width / 2, cy = b.top + b.height / 2;
      const at = (frac: number, rad: number) => { const a = -Math.PI / 2 + 2 * Math.PI * frac; return px(cx + rad * Math.cos(a), cy + rad * Math.sin(a)); };
      const arc = at(Math.min(v, 1) / 2, r);
      check("ring arc vs outside", svg, ratio(arc, at(Math.min(v, 1) / 2, r + s / 2 + 3)));
      if (v < 0.95) check("ring arc vs track", svg, ratio(arc, at((1 + v) / 2, r)));
    }
    return fails;
  }, CONTRAST_SRC);
}

test("UI components and graphics reach 3:1 (WCAG 1.4.11) on every page", async ({ page }) => {
  test.setTimeout(300_000);
  const failures: string[] = [];
  for (const path of await allPages(page)) {
    await settle(page, path);
    for (const f of await audit(page)) failures.push(`${path}: ${f}`);
  }
  const unique = [...new Set(failures)];
  expect(unique, unique.join("\n")).toEqual([]);
});

test("controls inside the overlays reach 3:1", async ({ page }) => {
  await settle(page, "/clients");
  await page.getByRole("button", { name: "New client" }).click();
  await page.waitForTimeout(400);
  const failures = await audit(page);
  await page.goto("/clients");
  await page.locator('a[href^="/clients/"]').first().click();
  await page.getByRole("button", { name: "Connections" }).click();
  await page.getByRole("button", { name: "Connect source" }).click();
  await page.getByRole("button", { name: /Slack internal app/ }).click();
  await page.waitForTimeout(400);
  failures.push(...(await audit(page)));
  expect(failures, failures.join("\n")).toEqual([]);
});
