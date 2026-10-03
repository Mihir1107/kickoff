/**
 * How each source is connected. The browser handles a credential for exactly one of them:
 * - "install": OAuth-capable (Slack distributed app, Teams admin consent). The UI only starts the
 *   server-side install flow (api.startInstall, M17 plan section 6); the grant goes provider → API, never via the page.
 * - "token": Slack internal-app tier. Slack gives no install grant to a third party for an app that
 *   lives in the customer's workspace, so their admin hands us its bot token once (M17 plan section 7).
 * - "upload": Slack exports, connected by validating an uploaded archive (ADR 0014).
 */
export type ConnectMethod = "install" | "token" | "upload";

export interface SourceOption {
  key: string;
  source: string; // ConnectionIn.source
  name: string;
  hint: string;
  method: ConnectMethod;
}

export const CONNECT_OPTIONS: SourceOption[] = [
  { key: "slack", source: "slack", name: "Slack", hint: "Install our app (OAuth). Grid, Business+, Pro, Free", method: "install" },
  { key: "teams", source: "teams", name: "Microsoft Teams", hint: "Tenant admin consent (Graph, application permissions)", method: "install" },
  { key: "slack_internal", source: "slack", name: "Slack internal app", hint: "Your admin's own app: one-time bot token", method: "token" },
  { key: "slack_export", source: "slack_export", name: "Slack export", hint: "Upload a workspace export ZIP", method: "upload" },
];

/** Display names for sources on connection cards (the backend decides what is connectable). */
export const SOURCE_NAMES: Record<string, string> = {
  slack: "Slack",
  slack_export: "Slack export",
  teams: "Microsoft Teams",
  dummy: "Golden dataset",
};
