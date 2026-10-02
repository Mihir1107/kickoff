/**
 * Display names for source ids (channels, custodians). There is no directory route yet
 * (ApiClient.directory is optional), so the book starts empty and unknown ids show as raw ids.
 * A directory response (or the demo client) fills it through `registerNames`.
 */
const conversations = new Map<string, string>();
const custodians = new Map<string, string>();

export function registerNames(d: { conversations: { id: string; name: string }[]; custodians: { id: string; name: string }[] }) {
  for (const c of d.conversations) conversations.set(c.id, c.name);
  for (const c of d.custodians) custodians.set(c.id, c.name);
}

export const conversationName = (id: string) => conversations.get(id) ?? id;
export const custodianName = (id: string) => custodians.get(id) ?? id;
export const scopeName = (type: string, id: string) => (type === "custodian" ? custodianName(id) : conversationName(id));
