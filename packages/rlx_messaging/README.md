# rlx_messaging

The `messaging` capability for botgear: one small wire contract that lets
the brain's conversation workflows talk through any chat platform, plus
`MessagingServiceBase`, the shared implementation the subprocess
platforms (`discord`, `slack`, `google_chat`) build on.

## The `messaging` capability

A service declares `implements: [messaging]` in its `package.yml` and
satisfies this contract on `/{type}/{proxy_id}/...`. It's a wire
contract — no base class is required: `chat` (in-process) implements it
by hand, the subprocess platforms via `MessagingServiceBase`.

### Actions

| Action | Signature | Returns |
|---|---|---|
| `listen` | `listen(timeout_seconds=8, channel=None)` | `{text, sender, channel, bearing: None, timeout: bool}` — a timeout returns, it doesn't raise |
| `speak` | `speak(text, channel=None)` | `{spoken, ts, channel, sent: bool}` |
| `list_channels` | `list_channels()` | `{channels: [{id, name}]}` |
| `send` | `send(text, channel=None)` | injects a **synthetic inbound** message (manual/test back-channel — never calls the platform) |

- `channel` omitted → the service's `default_channel`; `chat` has no
  channels and always returns `channel: None` (its `list_channels` is a
  single synthetic `local` entry).
- `sent: false` reports a soft platform failure (rate limit, network
  hiccup) without failing the calling workflow; a bad argument still raises.
- `bearing` is always `None` here; it stays in the shape so prompts written
  against chat/speech don't need special cases.

### Topics

| Topic | Direction | Payload |
|---|---|---|
| `inbox` | in | `{text, sender?, channel?, ts}` — every inbound message, real or simulated |
| `spoken` | out | `{text, channel, ts}` |
| `state` | out, retained | below |
| `control` | in | `{action, ...kwargs}` |

`inbox` keeps chat's bare `{text}` shape, so the brain's `exit_on_phrase`
watchers subscribe to any implementor's inbox unchanged.

```jsonc
// state
{
  "connected": true, "listening": false, "queued": 0,
  "last_inbox": "…", "last_spoken": "…", "default_channel": null,
  "capabilities": { "listen": true, "speak": true, "channels": true }
}
```

Consumers feature-detect with `capabilities` — a send-only implementor
says `listen: false` instead of letting `listen` time out forever.
Threads, reactions, attachments and rich embeds are outside the
capability; if a workflow ever needs them they belong as extra
service-specific methods.

## Platforms

| | `chat` | `discord` | `slack` | `google_chat` |
|---|---|---|---|---|
| speak | local | Gateway bot | Socket Mode bot | incoming webhook |
| listen | local | Gateway bot | Socket Mode bot | ✗ |
| list_channels | synthetic | guild text channels | `conversations.list` | ✗ |
| Auth | none | bot token | bot token + app-level token | webhook URL |
| Third-party deps | none | `discord.py` | `slack_sdk` | none (plain HTTP POST) |

None needs a public HTTPS endpoint: Discord's Gateway and Slack's Socket
Mode are connections the robot opens outward. Google Chat is send-only
because receiving needs a Google Cloud project plus Pub/Sub or a publicly
reachable HTTPS endpoint, which a robot on a home LAN doesn't have; an
incoming webhook needs neither.

### Configuration and setup gotchas

| Service | Config | Secrets |
|---|---|---|
| `discord` | `bot_token`, `default_channel` (channel id), `guild_id` (optional, narrows `list_channels`) | `bot_token` |
| `slack` | `bot_token` (`xoxb-…`), `app_token` (`xapp-…`), `default_channel` | both tokens |
| `google_chat` | `webhook_url`, `default_channel` (label only — one webhook = one space) | `webhook_url` (anyone holding it can post) |

- **Discord:** enable the privileged **Message Content Intent** for the
  application in the Developer Portal. Without it the bot connects fine but
  every inbound message arrives with empty content.
- **Slack:** enable Socket Mode and generate a separate app-level token
  with `connections:write` — two credentials.
- **Google Chat:** a space member creates the webhook under *Apps &
  integrations → Manage webhooks*; there's no programmatic path.

Secrets are stored encrypted (`Encrypted--…`) in config sets.

## `MessagingServiceBase`

Same pattern as `rlx_audio.SpeechServiceBase` and
`rlx_input.KeyboardServiceBase`: the base owns the generic machinery, a
thin platform subclass implements a few I/O hooks, and the base's
`@service_method`s are discovered on the subclass automatically.

```python
from rlx_messaging import MessagingConfig, MessagingServiceBase

class MyConfig(MessagingConfig):            # default_channel, echo_own_messages=False
    bot_token: SecretStr

class MyService(MessagingServiceBase):
    capabilities = {"listen": True, "speak": True, "channels": True}

    async def _connect(self) -> None: ...      # open the platform session (or validate a webhook)
    async def _disconnect(self) -> None: ...
    async def _send(self, channel, text) -> bool: ...   # becomes speak()'s `sent`
    async def _list_channels(self): ...        # only if capabilities["channels"]
    # call self._on_platform_message(text, sender, channel) from the event handler
```

The base provides a bounded inbox queue (drop-oldest on overflow, like
chat), `listen` / `speak` / `send` / `list_channels` with the shapes above
(including the `channel` filter on `listen`), and the retained `state`
snapshot.

## Brain integration

`conversation_session` / `conversation_servo` allow-lists and
`exit_on_phrase` watch every messaging inbox (`exit_on_phrase.listen_topic`
takes a list; the engine runs one watcher per topic), so the same workflow
runs over chat, Discord or Slack. Voice and Discord have been driven end to
end live.
