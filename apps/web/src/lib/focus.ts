import { useLayoutEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export const focusables = (root: HTMLElement) =>
  // tabIndex >= 0: a native control with tabindex="-1" (e.g. listbox options reached with the arrow keys)
  // is not a Tab stop; :disabled: framer adds tabindex to tappable buttons even when they are disabled.
  [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.tabIndex >= 0 && !el.matches(":disabled") && (el.offsetParent !== null || el === document.activeElement));

let openCount = 0;

/**
 * A modal surface (dialog, command palette) while `open`:
 * - remembers what had focus, moves focus inside (an [autofocus] element, else the first control);
 * - keeps Tab and Shift+Tab inside the surface;
 * - makes the app behind it inert (no focus, no clicks, hidden from assistive tech);
 * - on close, returns focus to where it was.
 * The surface must be portalled outside #root so that making #root inert does not disable it.
 */
export function useFocusTrap(open: boolean, ref: RefObject<HTMLElement | null>) {
  const returnTo = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  // Capture where focus is when the surface opens DURING RENDER: an autoFocus inside the surface moves
  // focus at commit, before any effect could read the opener. (A read, safe to repeat.)
  if (open && !wasOpen.current) returnTo.current = document.activeElement as HTMLElement | null;
  wasOpen.current = open;

  // A layout effect: the trap and inertness are in place before the browser handles any key event, so a
  // fast first Tab right after opening cannot slip out (a plain effect runs after paint).
  useLayoutEffect(() => {
    if (!open) return;
    const app = document.getElementById("root");
    openCount++;
    if (app) app.inert = true;

    const frame = requestAnimationFrame(() => {
      const root = ref.current;
      if (!root || root.contains(document.activeElement)) return;
      (root.querySelector<HTMLElement>("[autofocus]") ?? focusables(root)[0] ?? root).focus();
    });
    const onKey = (e: KeyboardEvent) => {
      const root = ref.current;
      if (e.key !== "Tab" || !root) return;
      const items = focusables(root);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0]!, last = items[items.length - 1]!;
      const inside = root.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKey, true);
      if (--openCount === 0 && app) app.inert = false;
      const target = returnTo.current;
      // After the surface unmounts, restore focus where the user was (if it still exists).
      requestAnimationFrame(() => {
        if (target && target.isConnected && !target.closest("[inert]")) target.focus();
      });
    };
  }, [open, ref]);
}
