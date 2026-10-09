# botgear Messaging Services — Design Spec

Status: **implemented (Phases 0–4).** This document defines the shared
`messaging` capability interface and the design for three new services
(`discord`, `slack`, `google_chat`) plus the additive changes made to
the existing `chat` service so all four are interchangeable. See §11 for
what's actually built vs. deferred to a later phase.

---

## 1. Motivation

Today `chat` (`repo/chat/1.0.0/chat.py`) is the only text-conversation
service, and the brain's `conversation_session` / `conversation_servo`
workflows are wired directly to its exact method names and topic shapes.
We want Discord, Slack, and Google Chat to be able to drop into those
same workflows (or new ones) as peers of `chat` — "attach to our chat
service" — without the brain engine needing type-specific code, and
without breaking `chat` or the two existing workflows that depend on it
today.

That means two things have to happen together:
1. Define a **minimal common-denominator wire contract** — simple enough
   that all four (a stateless in-process loopback, two full bots, and one
   webhook-only integration) can satisfy it honestly, without forcing the
   lowest-capability member (Google Chat, in its simple mode) to fake
   features it doesn't have.
2. Make the contract **additive over what `chat` already does**, so
   `chat.py` and the two brain workflows change in small, backward-
   compatible ways rather than a rewrite.

---

## 2. Baseline: `chat`'s contract today (must keep working)

Read directly from `repo/chat/1.0.0/chat.py`,
`tests/test_chat_service.py`, and the two brain workflows that consume it
(`repo/brain/1.0.0/workflows/conversation_session/`,
`.../conversation_servo/`). This is the contract any redesign must not
break:

| Surface | Shape |
|---|---|
| `listen(timeout_seconds=8, bearing=None)` | → `{"text": str, "bearing": float\|None, "timeout": bool}`. Times out silently (`timeout: True`, no exception) — `loop_on_timeout` in `workflow.yaml` re-dispatches `listen` on exactly this shape. |
| `speak(text: str)` | → `{"spoken": str, "ts": float}`. Publishes `{"text", "ts"}` to `/chat/{id}/spoken`. Non-blocking — no "wait for playback" semantics (unlike real TTS). |
| `send(text: str)` | Test/manual back-channel — injects `{"text", "ts"}` directly onto `/chat/{id}/inbox`, as if the operator typed it. Not a "send to platform" action. |
| `/chat/{id}/inbox` (topic, in) | `{"text": str, ...}` — operator/UI input. `workflow.yaml`'s `exit_on_phrase.listen_topic` subscribes here **directly**, bypassing the service entirely. |
| `/chat/{id}/spoken` (topic, out) | `{"text": str, "ts": float}` — not retained. |
| `/chat/{id}/state` (topic, out, retained) | `{"listening": bool, "queued": int, "last_inbox": str, "last_spoken": str}`. |
| `ChatConfig` | Empty — no fields today. |
| `package.yml` | No `implements`/`requires` — chat isn't part of the capability-discovery system at all today. |

Brain coupling specifics (`allowed_tools.yaml`, `workflow.yaml`,
`brain/schemas.py`): action names `listen`/`speak` on
`/chat/*/control`; `exit_on_phrase.listen_topic: /chat/+/inbox`;
`loop_on_timeout: {tool: listen, field: timeout, value: true}`. Both
workflows also list `/speech/*/control` and `/stt/*/control` as
alternative conversation-I/O providers in `allowed_tools.yaml` — so the
brain already tolerates "more than one type can offer this," just not
generically.

---

## 3. The `messaging` capability — wire contract

Follows the existing capability-interface convention in AGENTS.md
(`servo_controller`, `speech`, `signal_source`, etc.): a named contract
of `@service_method` action names + topic shapes, declared via
`implements: [messaging]` in `package.yml`. **No shared base class is
required to implement it** — it's a wire contract, satisfied equally by
an in-process `Service` (chat) or a subprocess `SubprocessService`
(discord/slack/google_chat). A shared *implementation helper* exists
separately for the three subprocess services (§5) because they happen to
need the same plumbing, not because the interface demands it.

Design principle: keep the **required** surface tiny and identical in
shape to what `chat` already returns, so nothing downstream breaks; add
new fields as optional/additive; push everything platform-specific
(channels, attachments, threads, reactions) into an *optional* tier that
a consumer must feature-detect via `state.capabilities` before relying
on.

### 3.1 Required tier (every implementor)

| Action | Signature | Returns |
|---|---|---|
| `listen` | `listen(timeout_seconds: float = 8, channel: str \| None = None)` | `{"text": str, "sender": str \| None, "channel": str \| None, "bearing": None, "timeout": bool}` |
| `speak` | `speak(text: str, channel: str \| None = None)` | `{"spoken": str, "ts": float, "channel": str \| None, "sent": bool}` |

- `bearing` stays in `listen`'s return (always `None` for messaging
  implementors) purely so existing prompts/workflows written against
  chat's shape don't need field-by-field special-casing — same reasoning
  the original chat docstring already gives for keeping it.
- `channel` is new and optional on both ends: omitted → the service's
  configured `default_channel`. `chat` has no channel concept, so it
  always echoes back `channel: None` and ignores the parameter.
- `sent: bool` on `speak`'s return distinguishes "accepted and handed to
  the platform" from "queued/rejected" for platforms where a send can
  fail (rate limit, invalid channel) without raising — a subprocess
  action is still a request/reply RPC, so a failed send should still be
  a `TypeError`/exception for the fatal cases (bad `text` type, per
  chat's existing behavior) but a soft platform failure (network hiccup)
  should surface as `sent: False` rather than crashing the calling
  workflow.

Topics (all under `/{type}/{proxy_id}/...`, same root convention as
every other service):

| Topic | Direction | Payload |
|---|---|---|
| `inbox` | in (subscribed) | `{"text": str, "sender": str \| None, "channel": str \| None, "ts": float}` |
| `spoken` | out (not retained) | `{"text": str, "channel": str \| None, "ts": float}` |
| `state` | out (retained) | see §3.3 |
| `control` | in | standard `{"action": ..., ...kwargs}` envelope |

`inbox` keeps the same bare shape `{"text": ...}` chat already uses
(with `sender`/`channel` as new optional fields) specifically so the
brain's existing `exit_on_phrase.listen_topic` raw-subscription pattern
keeps working unmodified against any implementor once its topic pattern
is generalized (§7.2) — it doesn't have to parse anything new.

### 3.2 Optional tier (platform-capable implementors only)

| Action | Signature | Returns | Who implements it |
|---|---|---|---|
| `list_channels` | `list_channels()` | `{"channels": [{"id": str, "name": str}]}` | discord, slack, google_chat (v2). `chat` returns a single synthetic `{"id": "local", "name": "local"}` entry so callers don't need a special case. |
| `send` | `send(text: str, channel: str \| None = None)` | same as `chat`'s existing `send` | Kept **only** as the "inject a synthetic inbound message" manual/test utility it already is on `chat` — every implementor gets it via the shared base for free; it is not a platform action and does not call any external API. |

Anything beyond this (reactions, threads, file attachments, mentions,
rich embeds) is explicitly **out of scope for v1** on every implementor.
The point of the capability is to be the smallest common denominator
that already covers what the brain's conversation workflows need; a
platform's richer native API remains reachable later as an *additional*,
non-`messaging`, service-specific method if a workflow ever needs it —
it just isn't part of this interface.

### 3.3 State — `/{type}/{proxy_id}/state` (retained)

```jsonc
{
  "connected": bool,           // platform session/webhook is live
  "listening": bool,           // a listen() call is currently parked
  "queued": int,                // inbox backlog depth
  "last_inbox": str,            // most recent inbound text (for UI display)
  "last_spoken": str,           // most recent outbound text
  "default_channel": str | null,
  "capabilities": {              // feature-detection for consumers
    "listen": bool,              // false for google_chat v1 (webhook-only)
    "speak": bool,
    "channels": bool             // list_channels is meaningful
  }
}
```

`capabilities` is the mechanism that lets a send-only implementor (v1
Google Chat) declare itself honestly rather than silently timing out
forever on `listen` with no explanation — a consumer (or a human reading
the Traffic/UI panel) can see `listen: false` and route accordingly.

---

## 4. Capability matrix — what v1 actually delivers per platform

| | `chat` | `discord` | `slack` | `google_chat` (v1) |
|---|---|---|---|---|
| `speak` (outbound) | ✅ local only | ✅ Gateway bot | ✅ Socket Mode bot | ✅ incoming webhook |
| `listen` (inbound) | ✅ local only | ✅ Gateway bot | ✅ Socket Mode bot | ❌ (see below) |
| `list_channels` | synthetic single entry | ✅ guild text channels | ✅ `conversations.list` | ❌ (webhook is bound to one space at creation time) |
| Auth | none | bot token | bot token + app-level token | webhook URL (per space) |
| Needs a public HTTPS endpoint? | no | no (Gateway is outbound-initiated) | no (Socket Mode is outbound-initiated) | no |
| New third-party deps | none | `discord.py` | `slack_sdk` (socket-mode extra) | **none** — plain HTTP POST |

**Why Google Chat is send-only in v1:** Google Chat's two ways to
*receive* events are (a) a Pub/Sub subscription or (b) an HTTP endpoint
Google calls directly — both require a real Google Cloud project and,
for (b), a publicly-reachable HTTPS URL registered with Google. `worke`
(and robot laptops in general) sits behind a home/LAN network with a
self-signed cert and no public DNS — there's nothing to register. An
**incoming webhook**, by contrast, needs zero Google Cloud setup beyond
clicking "Manage webhooks" in one Chat space and pasting a URL — it's a
plain `POST {"text": "..."}`, which is why it's the only Google Chat
integration going into v1. Full bidirectional Google Chat is a
documented Phase 2 (§9), not blocked by this design, just deferred.

Discord and Slack don't have this problem because their bots open an
**outbound** persistent connection (Gateway websocket / Socket Mode
websocket) to the platform — the robot laptop never needs to be
reachable from the internet, which is exactly why both are full
bidirectional in v1.

---

## 5. Shared implementation base — `packages/rlx_messaging`

Discord, Slack (and any future subprocess messaging service) share
enough plumbing that duplicating it three times would be a mistake. This
follows the **exact** precedent already validated twice in this codebase
— `rlx_audio.SpeechServiceBase` / `SttServiceBase` and
`rlx_input.KeyboardServiceBase`: a shared base class owns the generic
machinery, a thin per-platform subclass implements 2–4 small I/O hooks,
and `@service_method`s defined on the base are picked up automatically
on any subclass instance (`collect_methods` walks the live instance's
`dir()`, not a per-class registry — confirmed in `rlx_bus.methods`, no
manual re-registration needed).

```
packages/rlx_messaging/
  pyproject.toml              # pure Python, depends only on pydantic + rlx_bus
  src/rlx_messaging/
    __init__.py                # re-exports MessagingConfig, MessagingServiceBase
    base.py
```

`MessagingConfig(ServiceConfig)` — common fields every platform config
subclasses:

```python
class MessagingConfig(ServiceConfig):
    default_channel: Optional[str] = None
    echo_own_messages: bool = False   # usually False — avoid inbox feedback loops
```

`MessagingServiceBase(SubprocessService)` owns:
- A bounded `asyncio.Queue` inbox (same drop-oldest-on-overflow policy as
  chat's `_inbox_loop`) fed by `_on_platform_message(text, sender=None, channel=None)`
  — subclasses call this from their gateway/socket-mode event handler.
- `listen()` / `speak()` / `send()` implementing the exact shapes from
  §3.1/§3.2, including the `channel` filter on `listen()`.
- `list_channels()` — default returns `{"channels": []}`; a subclass
  overrides it if the platform supports enumeration.
- `_snapshot()` / `_publish_state()` — builds the §3.3 state payload,
  reading `self.capabilities` (a subclass-declared `ClassVar[Dict[str,bool]]`).
- Two–three abstract hooks a subclass must implement:
  - `async def _connect(self) -> None` — open the Gateway/Socket Mode
    connection (or, for a webhook-only integration, just validate config
    and mark `connected: True`).
  - `async def _disconnect(self) -> None`
  - `async def _send(self, channel: Optional[str], text: str) -> bool` —
    the actual platform API call; return value becomes `speak()`'s
    `sent` field.
  - `async def _list_channels(self) -> List[Dict[str, str]]` — optional,
    only if `capabilities["channels"]` is `True`.

`chat` does **not** use this base — it's in-process (`Service`, not
`SubprocessService`) and has no gateway/queue-of-its-own to share. It
satisfies the same wire contract by hand (§6). The capability is defined
at the wire level precisely so this split is fine — same reasoning
AGENTS.md already gives for `servo_controller`/`speech`: "no schema
enforcement — the contract is the agreed set of action names."

---

## 6. Required changes to `chat.py`

All additive, no breaking changes to existing fields or behavior:

1. `listen()` return gains `sender: None` and `channel: None` fields
   (existing consumers reading `text`/`bearing`/`timeout` are unaffected).
2. `speak()` gains an optional `channel: str | None = None` parameter
   (ignored) and its return gains `channel: None`, `sent: True`.
3. Add `list_channels()` → `{"channels": [{"id": "local", "name": "local"}]}`.
4. `state` payload gains `connected: True` (chat is always "connected" —
   it's local), `default_channel: None`,
   `capabilities: {"listen": true, "speak": true, "channels": false}`.
5. `package.yml` gains `implements: [messaging]`.
6. `ChatConfig` gains the same `default_channel`/`echo_own_messages`
   fields `MessagingConfig` defines, for shape parity — even though
   `default_channel` is always `None` for chat, having the field present
   means a future generic "messaging config" UI panel (§8) doesn't need
   a chat-specific exception.

No change to `inbox`/`spoken` topic shapes, no change to `send()`'s
existing test-backchannel behavior, no change to any currently-passing
test in `tests/test_chat_service.py`.

---

## 7. What has to change outside the new services

### 7.1 `installer.py` — new shared package

`install_pip()` in `src/botgear/runtime/installer.py` currently
hardcodes editable-installs of exactly four shared packages (`rlx_bus`
always; `rlx_audio`, `rlx_input`, `rlx_servo_cal` each skipped if not
found on disk). Adding `rlx_messaging` needs the same treatment: a
`_rlx_messaging_local_path()` resolver (copy `_rlx_input_local_path()`,
swap the name) plus a corresponding install-step block, so
`discord`/`slack`'s per-type venvs get `rlx_messaging` pre-installed the
same way `keyboard_local` gets `rlx_input`.

### 7.2 Brain workflow topic patterns — generalize off `/chat/*`

`repo/brain/1.0.0/workflows/{conversation_session,conversation_servo}/`:
- `allowed_tools.yaml` currently lists `/chat/*/control` (alongside
  `/speech/*/control`, `/stt/*/control`) — add `/discord/*/control`,
  `/slack/*/control`, `/google_chat/*/control`.
- `workflow.yaml`'s `exit_on_phrase.listen_topic: /chat/+/inbox` needs
  the same treatment — either list all four topics if the engine
  supports multiple `listen_topic` entries (needs checking — out of
  scope for this doc to assume), or widen to a single wildcard if the
  bus's `+`/`#` matching supports a multi-segment-type wildcard cleanly
  (it currently only wildcards path *segments*, not alternation across
  known type names, so this likely means literally listing all four
  rather than a clever single pattern).
- This is a **mechanical but required** change — without it, a running
  `discord`/`slack`/`google_chat` proxy is invisible to the brain's tool
  catalog even though it satisfies the `messaging` capability. Longer
  term, the tool catalog could resolve "every running proxy whose
  `service_meta` declares `implements: [messaging]`" dynamically instead
  of hardcoding topic patterns per type — flagged as a future brain-
  engine improvement, not part of this doc's phase.

### 7.3 `AGENTS.md` capability table

Add a `messaging` row to the "Promoted interfaces" table (the one
listing `servo_controller`, `speech`, `transcription`, etc.), pointing
at this doc and at `chat`/`discord`/`slack`/`google_chat` as
implementors — same pattern every other promoted interface follows.

Two stale references the research surfaced while reading AGENTS.md, worth
fixing in the same pass since we're touching this area:
- The "Subprocess (arduino / echo_http-style)" section's example bus
  client path (`arduino_service.bus_client.BusClient`) no longer exists
  — the real base is `rlx_bus.SubprocessService`/`BusClient.from_env()`.
- `echo_http` is described as a bus-integrated example; it's actually a
  bare FastAPI/uvicorn demo with no bus usage at all — not a good
  template to point new-service authors at for messaging-style services.

---

## 8. UI plan — one `ui/View.tsx` per new service

Same Option B modular-bundle mechanism every other service UI uses
(`repo/README.md`'s "Adding a UI" section) — `repo/<name>/1.0.0/ui/View.tsx`,
built via `node scripts/build-service-ui.mjs ../botgear/repo/<name>/1.0.0/ui`,
committed `dist/ui.js`, zero host-side registration needed (dynamic
`import()` fallback picks up any `service_meta_id` not in the static
registry).

Unlike `chat`'s view (pure transcript + composer, no config — chat has
no config fields), the three new services' views need **two** things
chat's doesn't:

1. **Same transcript + composer core** as `ChatFullView` — subscribe to
   `inbox`/`spoken`/`state`, render a two-column transcript, and (per
   §10.3) TWO separate composer buttons: "Simulate inbound" (raw
   `/inbox` publish, mirrors chat's UI exactly) and "Post as bot" (the
   `speak` control action — actually hits the platform). Implemented as
   built for `discord`/`slack`; `google_chat` (send-only) drops
   "Simulate inbound" entirely since it has no inbound.
2. **A connection/config strip** the other views don't need:
   `connected` indicator (from `state.connected`) and `default_channel`
   display. **Scope cut from the original plan**: no live channel-picker
   dropdown in v1 — `list_channels()` is a request/response action, not
   a retained topic, and wiring a dropdown would need `reply_to`
   plumbing the view layer doesn't have. Shipped instead: `default_channel`
   shown as a placeholder plus a free-text per-message channel-id
   override field. A real dropdown is a natural follow-up once that
   reply plumbing exists generically (it'd help other views too).
   Editing the actual secret (bot token / webhook URL) happens through
   the existing generic service-config panel (proxy creation / settings)
   — not a bespoke field in `View.tsx` — see §10.1 for the current state
   of that panel (dead code, not wired up yet).

---

## 9. Per-service config

| Service | Config fields | Secret(s) |
|---|---|---|
| `discord` | `bot_token`, `default_channel` (channel id), `guild_id` (optional, narrows `list_channels`) | `bot_token` |
| `slack` | `bot_token` (`xoxb-...`), `app_token` (`xapp-...`, Socket Mode), `default_channel` (channel id) | both tokens |
| `google_chat` (v1) | `webhook_url`, `default_channel` (cosmetic label only — v1 is one webhook = one space) | `webhook_url` (it's bearer-equivalent — anyone with the URL can post) |

Operational gotchas worth documenting in each service's own
`package.yml` description (mirroring how `speech_local`'s description
documents its contract):
- **Discord**: the bot's "Message Content Intent" is a *privileged
  intent* that must be explicitly enabled in the Discord Developer
  Portal for the application, or `_connect()` will succeed but every
  inbound message will arrive with an empty `content` field — a classic
  silent-failure trap worth a doc callout so it isn't rediscovered the
  hard way.
- **Slack**: Socket Mode requires the app to have Socket Mode enabled
  *and* an app-level token with the `connections:write` scope generated
  separately from the bot token — two credentials, not one.
- **Google Chat**: the webhook URL is created per-space by a *space
  member* via "Apps & integrations → Manage webhooks" — there's no
  programmatic creation path for v1; it's a manual one-time step by
  whoever owns that Chat space.

---

## 10. Open questions — resolved

1. **Secret storage/display → real `SecretStr` fields, plus one targeted
   leak fix.** `bot_token`/`app_token`/`webhook_url` are all Pydantic
   `SecretStr` (same precedent as `brain.service.BrainConfig`'s
   `anthropic_api_key`) — encrypted at rest via the existing generic
   `config_sets.py` encrypt/decrypt-on-save path (type-driven, needed no
   changes). Investigating the live-broadcast side turned up a real,
   pre-existing gap: `discovery._handle_config_patch` and
   `lifecycle.reload_one_service_config` both re-publish a **decrypted**
   config dict onto the retained `/service_proxy/{id}/config_state`
   topic, which any WS-connected browser can subscribe to — plaintext,
   regardless of field type. Fixed via a new
   `discovery._mask_secret_fields()` used at both call sites: it
   resolves the proxy's `config_class` via the new
   `boot.resolve_config_class()` (reuses the same dynamic-file-load path
   `_row_to_config_instance` already used for the yml migrator — works
   for subprocess modules too, AS LONG AS they keep third-party SDK
   imports lazy, see §5/§9) and redacts any `SecretStr` field; falls
   back to a small naming-convention denylist
   (`token`/`secret`/`password`/`webhook_url`/`api_key`) when the class
   can't be resolved (e.g. arduino, which imports `pymata4` at module
   scope). **Known limitation, not fully closed**: this fixes the one
   leak path these new services actually exercise live. Several other
   pre-existing plaintext-egress points were found and are NOT fixed
   here (out of scope for a messaging feature) — `GET /v1/service-proxy/*`,
   `GET /v1/service-config/*`, and `GET /admin/table/*` (schema-agnostic,
   dumps every table raw) all still return unmasked config values, and
   the frontend's `ConfigWizard`/`InstallWizard` components (the only
   per-instance config-editing form) are dead code today — not imported
   anywhere in `apps/botgear_ui/src` — so there is currently no wired
   UI path to even edit a running proxy's config; it has to be set at
   creation time. Closing all of that is a separate, botgear-wide
   secrets-hardening pass, not scoped to this doc.
2. **`slack_sdk` vs `slack_bolt` → `slack_sdk[aiohttp]`.** Confirmed and
   implemented — `slack_service` uses
   `slack_sdk.socket_mode.aiohttp.SocketModeClient` directly, async-
   native, runs in the service's own event loop.
3. **Composer semantics in the UI → both, as separate buttons.**
   Implemented in `discord`'s (and `slack`'s, same shape) `View.tsx`:
   "Simulate inbound" (publishes straight to `/inbox`) and "Post as bot"
   (calls the `speak` control action) are two distinct buttons.
4. **Brain engine capability/multi-topic support → checked, doesn't
   exist, added the minimal fix.** Confirmed by reading
   `workflow_engine.py`/`safety_gate.py`/`schemas.py`: `allowed_tools.yaml`
   matching is pure literal/wildcarded topic-pattern matching against
   live proxies (no `implements`-based resolution anywhere in the
   brain's runtime path — `implements` is only consulted today by
   `config_sets.py` for start-order dependencies). Literal enumeration
   was the right call there (four more rows in each workflow's
   `allowed_tools.yaml`, zero code changes). `exit_on_phrase.listen_topic`
   genuinely couldn't watch multiple topics before — it was a single
   `Optional[str]`, and MQTT wildcard grammar can't alternate between
   different literal type-name segments in one pattern. Extended it to
   `Optional[Union[str, List[str]]]` (`brain/schemas.py`) with a
   `listen_topics` normalizing property, and `workflow_engine.py`'s
   watcher now fans out one `_watch_exit_topic` coroutine per topic via
   `asyncio.gather` — cancelling the run cancels every sibling watcher
   together through the existing `run().finally` path, so no manual
   cross-task signaling was needed. Both bundled workflows'
   `workflow.yaml` now list all four `/{type}/+/inbox` topics.
5. **Multi-instance channel routing → confirmed, `default_channel` is
   enough for v1.** No per-proxy channel allow-list was added; a second
   `discord` proxy (a second bot, or one watching a second server) is
   just a second proxy with its own `default_channel`.

---

## 11. Phased implementation plan

- [x] **Phase 0 — foundations**
  - [x] Resolve open questions §10.1 and §10.4 (secret masking, brain
        engine capability check) — both affect shapes decided in later
        phases.
  - [x] Create `packages/rlx_messaging` (`MessagingConfig`,
        `MessagingServiceBase`) per §5.
  - [x] Extend `installer.py` for the new shared package per §7.1.
  - [x] Land the additive `chat.py`/`package.yml` changes per §6 — this
        alone makes `chat` a spec-compliant `messaging` implementor and
        is fully backward compatible, independently testable/shippable
        before any new service exists.
  - [x] Add the `messaging` capability row + fix the two stale
        references in `AGENTS.md` per §7.3.

- [x] **Phase 1 — Discord** (full bidirectional, sets the pattern for Slack)
  - [x] `repo/discord/1.0.0/` bundle: `package.yml`
        (`implements: [messaging]`), `pyproject.toml` (`discord.py`),
        `src/discord_service/{service.py,__main__.py}` subclassing
        `MessagingServiceBase`.
  - [x] `ui/View.tsx` per §8.
  - [ ] Manual smoke test against a real (throwaway/test) Discord bot +
        server — **not done**, needs a real bot token/server from you;
        unit tests cover the orchestration logic (`_send`/`_list_channels`)
        against a fake client, but nothing has exercised the real
        `discord.py` Gateway connection yet.

- [x] **Phase 2 — Slack** (mirrors Discord's shape, Socket Mode instead
      of Gateway)
  - [x] `repo/slack/1.0.0/` bundle + UI, same structure as Phase 1.
  - [ ] Manual smoke test against a real Slack app — **not done**, same
        reason as Discord above (needs real bot + app-level tokens).

- [x] **Phase 3 — Google Chat v1 (webhook, send-only)**
  - [x] `repo/google_chat/1.0.0/` bundle — zero third-party deps (stdlib
        `urllib` only).
  - [x] UI: no channel picker, no composer's "simulate inbound" affordance
        (there's no inbound), send-only banner shown plainly.
  - [ ] Manual smoke test against a real webhook URL — **not done**.

- [x] **Phase 4 — brain integration**
  - [x] Generalize `allowed_tools.yaml`/`workflow.yaml` topic patterns
        per §7.2 for both existing workflows — plus the `ExitOnPhrase`
        schema/engine change §10.4 turned out to actually require
        (single-topic → list, with a per-topic watcher fan-out in
        `workflow_engine.py`).
  - [x] End-to-end test: `tests/test_brain.py`'s
        `test_conversation_session_multi_turn_until_keyword` now proves
        the multi-topic `exit_on_phrase` fan-out against a live engine
        run (still exercised via `chat`'s topic, one of the four now
        watched) — a real Discord/Slack proxy run is still open, same
        blocker as the Phase 1/2 manual smoke tests above.

Full existing test suite (898 tests) passes with all of the above
landed; 33 new tests cover `rlx_messaging.MessagingServiceBase` and the
three concrete services' non-SDK orchestration logic.

- [ ] **Phase 5 (future, not scheduled)** — full bidirectional Google
      Chat (Pub/Sub or public HTTP endpoint), richer per-platform
      features (threads, reactions, attachments) as service-specific
      extensions beyond the `messaging` capability, multi-proxy channel
      routing (§10.5).
