# MCP gateway

External AI agents (Claude Code, Codex, the Claude desktop app, anything
that speaks MCP) drive botgear's live services through one endpoint:

    https://<host>:8998/v1/mcp        streamable HTTP · JSON responses · stateless

Phase 1 of [`docs/AGENT_ROADMAP.md`](../../../docs/AGENT_ROADMAP.md). Built on
[`agent_surface`](../agent_surface/), the same catalog, policy and invocation
brain uses, so an external agent is held to the same rules as brain.

## Connect a client

Validated 2026-10-04 with Claude Code (`claude mcp add` → `botgear_list_capabilities`).

1. **Create an API client** in the UI (**Tools → API Clients**) and copy its
   secret. Its *name* selects its profile (below); it shows as `client` in
   the audit log.
2. **Add the server** (Claude Code; `--scope user` makes it available in
   every project, default is the current one):

   ```bash
   claude mcp add --transport http botgear https://localhost:8998/v1/mcp \
     --header "X-API-Key: <secret>"
   ```

3. **Trust the backend's self-signed certificate** — see below. Without it
   the client fails the TLS handshake and the server shows as failed in
   `claude mcp list` / `/mcp`.
4. Ask the agent to call `botgear_list_capabilities` first.

A logged-in user's Bearer token works too (`Authorization: Bearer …`); API
clients are the intended route for agents.

### The self-signed certificate

The backend serves HTTPS with `apps/botgear/ssl/cert.pem`, generated on first
boot (only when the file is missing; machine-local, gitignored). As
generated it is:

- **self-signed**, so no client trusts it by default, and
- **for `localhost` only**: `CN=localhost`, no subjectAltName, valid one year.

**Same machine as the backend (the validated setup).** Claude Code runs on
Node, which reads extra trusted CAs from `NODE_EXTRA_CA_CERTS` *only when the
process starts*, so export it in the shell that launches `claude`. Setting it
in Claude Code's `settings.json` `env` is too late.

```bash
export NODE_EXTRA_CA_CERTS=/home/gperry/github/cloudseeder/apps/botgear/ssl/cert.pem
claude
# or once, in ~/.bashrc:  alias claude-botgear='NODE_EXTRA_CA_CERTS=…/ssl/cert.pem claude'
```

Other clients: Python (`httpx`/`requests`) take the file as their CA bundle
(`verify=".../ssl/cert.pem"`, or `SSL_CERT_FILE`); curl: `--cacert
ssl/cert.pem` (`-k` skips verification — tests only).

**Another machine.** Trusting the cert is not enough: the client connects to
`worke.local` / `192.168.x.y`, the cert only names `localhost`, and the
hostname check fails. Either:

- **SSH tunnel (simplest, no cert changes):**
  `ssh -N -L 8998:localhost:8998 <backend-host>`, then use
  `https://localhost:8998/v1/mcp` and the backend's `cert.pem` (copy it over)
  exactly as above.
- **Reissue the cert with the backend's names**, on the backend host. It's
  kept across restarts because the backend only generates one when none
  exists:

  ```bash
  cd apps/botgear
  openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
    -keyout ssl/key.pem -out ssl/cert.pem -subj "/CN=worke.local" \
    -addext "subjectAltName=DNS:worke.local,DNS:localhost,IP:127.0.0.1,IP:192.168.0.164"
  ```

  Restart the backend **and** the UI dev server (Vite reuses the same
  files), re-accept the new cert in the browser, and copy the new
  `cert.pem` to each client machine for `NODE_EXTRA_CA_CERTS`.

`NODE_TLS_REJECT_UNAUTHORIZED=0` also "works", but it turns off certificate
checking for everything that Claude Code process talks to (including the
model API). Don't use it outside a throwaway test.

**When the cert expires** (one year from first boot; `openssl x509 -in
ssl/cert.pem -noout -enddate`), delete both `ssl/*.pem`, restart the backend
to regenerate, and redistribute it to clients.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `/mcp` shows botgear failed, TLS / "self-signed certificate" error | `NODE_EXTRA_CA_CERTS` not set in the shell that started `claude` |
| "Hostname/IP does not match certificate" | connecting by a name other than `localhost`: tunnel or reissue (above) |
| HTTP 401 `send X-API-Key …` / `Invalid API key` | header missing, or the API client was deleted / secret changed |
| Connected, but a tool is missing | the client's profile blocks it, or the service isn't running (`botgear_list_capabilities`) |
| Every call refused with `HALTED` | someone pressed Stop / said "stop": Resume in the UI |

## What the agent sees

| Tool | What it is |
|---|---|
| `<service>__<action>` | Every live service action the client's profile allows, e.g. `switch-1__power_set`, `mouth__speak`. Description = the method docstring + service/topic; input schema = the signature-derived `args_schema`. Read-only actions (`list_`, `get_`, `describe_`, …) carry `readOnlyHint`, so clients can auto-approve them |
| `botgear_list_capabilities` | Capabilities → live instances → tool names, plus whether the runtime is HALTed |
| `botgear_get_state` | A service's retained state (`service="switch-1"`), the runtime's (`"runtime"`) or HALT status (`"halt"`) without calling anything |
| `botgear_halt` | Runtime-wide HALT (`level: stop \| emergency`), always allowed |

The list is recomputed from the live catalog on every `tools/list`, so new
or released services show up on the client's next list (stateless: no
`tools/list_changed` push).

## Policy

Every call goes through, in order:

1. **Auth:** the REST API's own rules (`X-API-Key` → API client; Bearer →
   user). No credentials → 401.
2. **HALT:** while `/runtime/halt` is in force, every action except
   `botgear_halt` is refused, whatever the profile says.
3. **Profile:** a `ToolPolicy` (same format and evaluation as a brain
   workflow's `allowed_tools.yaml`; blocked always wins):

   ```
   <data_dir>/mcp/profiles/<client>.yaml    API-client name (else id); users: email
   <data_dir>/mcp/profiles/default.yaml     everyone without their own file
   built-in DEFAULT_PROFILE                 when neither exists
   ```

   The built-in default (`profiles.py`): `default_policy: allow` plus the
   shared hardware excludes (movement, arm, system, **video**, sabertooth,
   motor_control — so no camera frames leave through the gateway), brain's
   self-control actions (start/cancel/approve runs, backend credentials,
   workflow config), every service's `set_config` / `save_config` /
   `reload_config`, and the agent service's `start_run` / `cancel_run` (no
   agent launching further agents). A malformed profile denies everything.

   Example: a client that may only read state and flip switches:

   ```yaml
   # <data_dir>/mcp/profiles/kitchen-assistant.yaml
   allowed:
     - topic: /tasmota/*/control
       actions: [power_set, power_toggle, list_relays]
   ```
4. **Live:** the action must exist on a running service right now.

`get_state` follows the profile too (a service the client can't call isn't
readable), and `/security/…` state is never readable.

## Audit

Every call, refused or not: one JSON line in `<data_dir>/mcp/calls.jsonl`
(`ts, client, tool, topic, action, args, ok, detail, duration_ms`) and the
same record on the bus at `/mcp/gateway/calls`.

## Security notes

- Annotations are hints. The gateway enforces policy itself; a client may
  ignore `readOnlyHint` and the gateway still refuses what the profile
  blocks.
- Text coming back from services (device names, chat messages, transcripts)
  reaches the agent as tool output. Treat agents with broad profiles as able
  to be steered by that text (prompt injection), and keep their profiles
  narrow.
- Not yet: per-client rate limits, MCP resources/subscriptions,
  `tools/list_changed` push, elicitation for confirm-before-act, OAuth.
