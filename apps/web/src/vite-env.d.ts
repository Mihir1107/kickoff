/// <reference types="vite/client" />
interface ImportMetaEnv {
  /** "demo" (dev server only, the default there) or "http". A production build is always "http". */
  readonly VITE_API_MODE?: "demo" | "http";
}
