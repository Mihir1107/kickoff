#!/usr/bin/env node
/**
 * WCAG 2.1 AA text contrast for the glass UI (run by `npm run lint`).
 *
 * The panels are translucent, so text is checked against the LIGHTEST measured panel background
 * (worst case), sampled from screenshots of every page (see README, "Accessibility"). Every
 * `text-white/N` utility in src must reach 4.5:1 there, as must the colour tokens used for text.
 * A lower opacity is allowed only on decorative elements: the same line must carry `aria-hidden`.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const WORST_BG = [38, 50, 54]; // lightest measured panel pixel behind small text was rgb(35,48,52); this adds margin
const AA = 4.5;

const lin = (c) => {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};
const over = (fg, alpha, bg) => fg.map((c, i) => Math.round(c * alpha + bg[i] * (1 - alpha)));
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

const failures = [];

// Colour tokens used as text colours (index.css :root and @theme).
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const TEXT_TOKENS = ["--ok", "--live", "--warn", "--bad", "--muted", "--info", "--violet", "--orange", "--color-mint", "--color-cyan", "--color-iris", "--color-amber", "--color-rose", "--color-info", "--eyebrow", "--placeholder"];
for (const t of TEXT_TOKENS) {
  const m = css.match(new RegExp(`${t}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) { failures.push(`token ${t} not found in index.css`); continue; }
  const r = ratio(hex(m[1]), WORST_BG);
  if (r < AA) failures.push(`token ${t} ${m[1]}: ${r.toFixed(2)}:1 < ${AA}:1`);
}

// text-white/N utilities across the source.
const files = [];
const walk = (d) => readdirSync(d).forEach((f) => { const p = join(d, f); statSync(p).isDirectory() ? walk(p) : /\.tsx?$/.test(p) && files.push(p); });
walk(new URL("../src", import.meta.url).pathname);
let checked = 0;
for (const f of files) {
  readFileSync(f, "utf8").split("\n").forEach((line, i) => {
    for (const m of line.matchAll(/(?<![-\w])text-white\/(\d+)/g)) {
      checked++;
      const r = ratio(over([255, 255, 255], Number(m[1]) / 100, WORST_BG), WORST_BG);
      if (r < AA && !line.includes("aria-hidden")) failures.push(`${f.split("/src/")[1]}:${i + 1} text-white/${m[1]}: ${r.toFixed(2)}:1 < ${AA}:1`);
    }
  });
}

if (failures.length) {
  console.error(`Contrast check failed (WCAG AA ${AA}:1 against rgb(${WORST_BG})):\n  ` + failures.join("\n  "));
  process.exit(1);
}
console.log(`Contrast OK: ${TEXT_TOKENS.length} tokens and ${checked} text-white utilities reach ${AA}:1 against rgb(${WORST_BG}).`);
