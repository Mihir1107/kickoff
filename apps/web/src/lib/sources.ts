/** Source catalogue for the UI (labels only; the backend decides what is connectable). */
export const SOURCES = [
  { id: "slack", name: "Slack", hint: "Enterprise Grid, Business+, Pro, Free" },
  { id: "slack_export", name: "Slack export", hint: "Upload a workspace export ZIP" },
  { id: "teams", name: "Microsoft Teams", hint: "Graph API · client credentials + certificate" },
  { id: "dummy", name: "Golden dataset", hint: "Deterministic oracle connector" },
] as const;
