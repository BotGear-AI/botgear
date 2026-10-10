# TODO — messaging: remaining work

The `messaging` capability and its four implementors (`chat`, `discord`,
`slack`, `google_chat`) are built and wired into the brain (Phases 0–4) —
documented in `packages/rlx_messaging/README.md`. Discord has been driven
end to end live. Verified 2026-10-10; open items:

## Live smoke tests
- **Slack** — run against a real Slack app (bot token + app-level token,
  Socket Mode): connect, `list_channels`, `speak`, inbound → `listen`, and a
  `conversation_session` run over it.
- **Google Chat** — post via a real incoming-webhook URL; confirm
  `state.capabilities.listen == false` is reported.

## Phase 5 (not scheduled)
- Bidirectional Google Chat (Pub/Sub subscription or a public HTTPS
  endpoint registered with Google — needs a Google Cloud project).
- Richer per-platform features (threads, reactions, attachments) as
  service-specific methods outside the `messaging` capability.
- Multi-proxy channel routing — several messaging proxies live at once,
  with a workflow choosing which one replies.
