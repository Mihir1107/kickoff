import type { Page } from "@playwright/test";

/** Every page of the app, for the audits that must cover all of them (demo data; live stack later). */
export const STATIC_PAGES = ["/", "/clients", "/collections", "/exports", "/custody", "/access", "/collections/new", "/login"];

export async function allPages(page: Page): Promise<string[]> {
  await page.goto("/collections");
  await page.locator('a[href^="/jobs/"]').first().waitFor();
  const jobs = await page.$$eval('a[href^="/jobs/"]', (as) => [...new Set(as.map((a) => a.getAttribute("href")!))].slice(0, 3));
  await page.goto("/clients");
  await page.locator('a[href^="/clients/"]').first().waitFor();
  const client = await page.$eval('a[href^="/clients/"]', (a) => a.getAttribute("href")!);
  await page.goto(client);
  await page.locator('a[href^="/matters/"]').first().waitFor();
  const matter = await page.$eval('a[href^="/matters/"]', (a) => a.getAttribute("href")!);
  return [...STATIC_PAGES, ...jobs, client, matter, "/does-not-exist"];
}

/**
 * Waits until the page has data and every entrance animation has FINISHED (not a fixed sleep: a busy
 * machine runs them late). Still = no finite CSS/Web Animation running, and the inline opacity and
 * transform values framer-motion writes are unchanged over consecutive samples. Infinite decoration
 * (aurora, pulses, and anything under [data-decorative-motion], e.g. the login hash rain) is ignored.
 */
export async function settle(page: Page, path: string) {
  await page.goto(path);
  await page.waitForLoadState("networkidle");
  await page.waitForFunction(
    () => {
      const w = window as unknown as { __settle?: { sig: string; same: number } };
      const finite = document.getAnimations().filter((a) => a.playState === "running" && a.effect?.getComputedTiming().iterations !== Infinity);
      let sig = "";
      for (const el of document.querySelectorAll<HTMLElement>("[style]")) if (!el.closest("[data-decorative-motion]")) sig += `${el.style.opacity}|${el.style.transform}|${el.style.filter};`;
      const st = (w.__settle ??= { sig: "", same: 0 });
      st.same = finite.length === 0 && sig === st.sig ? st.same + 1 : 0;
      st.sig = sig;
      return st.same >= 3;
    },
    undefined,
    { polling: 150, timeout: 20_000 },
  );
}
