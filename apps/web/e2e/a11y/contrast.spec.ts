import { expect, test, type Page } from "@playwright/test";
import { allPages, settle } from "../support/pages";

/**
 * WCAG 2.1 AA text contrast, measured on rendered pixels (the glass panels are translucent, so class
 * names alone cannot say what is behind the text).
 *
 * For each page: record every visible text run's computed colour, effective opacity and its own line
 * boxes; then hide all text, screenshot, and take the 95th-percentile luminance of the pixels under
 * those boxes as its background. 4.5:1 for body text, 3:1 for large text (>= 24px, or >= 18.66px bold).
 * Gradient display text and aria-hidden / disabled / mid-animation (opacity < 0.5) content are skipped.
 * The static check (scripts/check-contrast.mjs, `npm run lint`) uses the lightest background found here.
 */

interface Finding { ratio: number; need: number; text: string; cls: string; bg: number[] }

async function audit(page: Page): Promise<Finding[]> {
  await page.evaluate(() => {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 1;
    const cx = cv.getContext("2d", { willReadFrequently: true })!;
    const rgba = (s: string) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = s; cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data; return [d[0]!, d[1]!, d[2]!, d[3]! / 255]; };
    const els: unknown[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const t = walker.currentNode;
      if (!t.textContent?.trim()) continue;
      const el = t.parentElement;
      if (!el || el.closest('[aria-hidden="true"],button:disabled,.sr-only,option,select,script,style,svg,.text-gradient')) continue;
      const range = document.createRange();
      range.selectNodeContents(t);
      // Clip each line box to every scrolling/clipping ancestor: text scrolled out of a container is not
      // visible, and the pixels at its coordinates belong to something else.
      let clip = { l: 0, t: 0, r: innerWidth, b: innerHeight };
      for (let n: HTMLElement | null = el; n; n = n.parentElement) { // the element itself too: ellipsis truncation clips
        const o = getComputedStyle(n);
        if (o.overflowX !== "visible" || o.overflowY !== "visible") {
          const b = n.getBoundingClientRect();
          clip = { l: Math.max(clip.l, b.left), t: Math.max(clip.t, b.top), r: Math.min(clip.r, b.right), b: Math.min(clip.b, b.bottom) };
        }
      }
      const rects = [...range.getClientRects()]
        .map((r) => { const l = Math.max(r.left, clip.l), tp = Math.max(r.top, clip.t); return new DOMRect(l, tp, Math.min(r.right, clip.r) - l, Math.min(r.bottom, clip.b) - tp); })
        .filter((r) => r.width > 1 && r.height > 1);
      if (!rects.length) continue;
      let op = 1;
      for (let n: Element | null = el; n; n = n.parentElement) op *= parseFloat(getComputedStyle(n).opacity);
      if (op < 0.5) continue;
      const cs = getComputedStyle(el);
      els.push({ text: t.textContent.trim().slice(0, 40), rgba: rgba(cs.color), op, rects: rects.map((r) => [r.x, r.y, r.width, r.height]), size: parseFloat(cs.fontSize), weight: Number(cs.fontWeight), cls: String(el.className).slice(0, 80) || el.tagName });
    }
    (window as unknown as { __els: unknown[] }).__els = els;
  });
  await page.addStyleTag({ content: "*{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;caret-color:transparent!important} .text-gradient{background:none!important} input::placeholder{color:transparent!important}" });
  await page.waitForTimeout(250);
  const png = (await page.screenshot()).toString("base64");
  return page.evaluate(async (png) => {
    type El = { text: string; rgba: number[]; op: number; rects: number[][]; size: number; weight: number; cls: string };
    const img = new Image();
    img.src = `data:image/png;base64,${png}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const g = c.getContext("2d", { willReadFrequently: true })!;
    g.drawImage(img, 0, 0);
    const lin = (v: number) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const L = (p: number[]) => 0.2126 * lin(p[0]!) + 0.7152 * lin(p[1]!) + 0.0722 * lin(p[2]!);
    const out: { ratio: number; need: number; text: string; cls: string; bg: number[] }[] = [];
    for (const e of (window as unknown as { __els: El[] }).__els) {
      const px: number[][] = [];
      for (const [x, y, w, h] of e.rects) {
        const d = g.getImageData(Math.floor(x!), Math.floor(y!), Math.max(1, Math.floor(w!)), Math.max(1, Math.floor(h!))).data;
        for (let i = 0; i < d.length; i += 8) px.push([d[i]!, d[i + 1]!, d[i + 2]!]);
      }
      px.sort((a, b) => L(a) - L(b));
      const bg = px[Math.floor(px.length * 0.95)] ?? px[px.length - 1]!;
      const a = e.rgba[3]! * e.op;
      const fg = [0, 1, 2].map((i) => e.rgba[i]! * a + bg[i]! * (1 - a));
      const ratio = (Math.max(L(fg), L(bg)) + 0.05) / (Math.min(L(fg), L(bg)) + 0.05);
      const need = e.size >= 24 || (e.size >= 18.66 && e.weight >= 700) ? 3 : 4.5;
      if (ratio < need) out.push({ ratio: Math.round(ratio * 100) / 100, need, text: e.text, cls: e.cls, bg });
    }
    return out;
  }, png);
}

test("every text run meets WCAG AA contrast on every page", async ({ page }) => {
  test.setTimeout(240_000);
  const pages = await allPages(page);
  const failures: string[] = [];
  for (const path of pages) {
    await settle(page, path);
    for (const f of await audit(page)) failures.push(`${path}: ${f.ratio}:1 < ${f.need}:1 "${f.text}" bg=rgb(${f.bg}) .${f.cls}`);
  }
  expect(failures, failures.join("\n")).toEqual([]);
});
