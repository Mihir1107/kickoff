import { MotionGlobalConfig } from "framer-motion";
import { useSyncExternalStore } from "react";

/**
 * prefers-reduced-motion, live. When set:
 * - every framer-motion animation completes instantly (MotionGlobalConfig.skipAnimations), which
 *   removes blur/slide page transitions, rings, sparklines and layout morphs;
 * - components drop effects that are not framer animations: the cursor spotlight, rolling numbers,
 *   the chain verification sweep, SMIL pulses, the login hash rain (each checks useReducedMotion());
 * - CSS keyframes and transitions are neutralised by the media query in index.css.
 */
const query = typeof window !== "undefined" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

function apply() {
  MotionGlobalConfig.skipAnimations = !!query?.matches;
}
apply();
query?.addEventListener("change", apply);

const subscribe = (cb: () => void) => {
  query?.addEventListener("change", cb);
  return () => query?.removeEventListener("change", cb);
};

export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, () => !!query?.matches, () => false);
}
