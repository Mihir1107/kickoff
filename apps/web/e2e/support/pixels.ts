import type { Page } from "@playwright/test";

/**
 * Rendered-pixel helpers. A screenshot is decoded in the page (canvas) so measurements see exactly
 * what the user sees: translucent glass, gradients, the aurora behind everything.
 */

/** WCAG relative luminance and contrast ratio, in the page and in Node alike. */
export const CONTRAST_SRC = `
  const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lum = (p) => 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2]);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
`;

/** Hide every glyph (text, placeholders, gradient text) so it cannot pollute background samples. */
export const HIDE_TEXT =
  "*{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important;caret-color:transparent!important} .text-gradient{background:none!important} input::placeholder{color:transparent!important}";

/** Load the current viewport into a canvas the page can sample as `window.__px(x, y)`. */
export async function capture(page: Page): Promise<void> {
  const png = (await page.screenshot()).toString("base64");
  await page.evaluate(async (png) => {
    const img = new Image();
    img.src = `data:image/png;base64,${png}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const g = c.getContext("2d", { willReadFrequently: true })!;
    g.drawImage(img, 0, 0);
    const data = g.getImageData(0, 0, c.width, c.height).data;
    const w = c.width;
    (window as unknown as { __px: (x: number, y: number) => number[] }).__px = (x, y) => {
      const i = (Math.round(y) * w + Math.round(x)) * 4;
      return [data[i]!, data[i + 1]!, data[i + 2]!];
    };
  }, png);
}
