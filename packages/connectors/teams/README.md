# edisc-connector-teams (stub)

**Phase 4. Do not implement yet.**

- Application permissions `Chat.Read.All`, `ChannelMessage.Read.All` with admin consent.
- `users/{id}/chats/getAllMessages` (+ delta for incremental).
- `teams/{id}/channels/getAllMessages` (filter channel client-side).
- Deleted teams endpoint; retention hold support.
- Dedup by message id + etag.
- SharePoint/OneDrive files as child items, with version capture.
