# Agents and botgear: roadmap

The canonical plan for how AI agents drive botgear: what `brain` is, what
`packages/agent_harness` is, the gap between them, and the phases that close
it. Supersedes `AGENT_SERVICE_STRATEGY.md` (2026-10-03), whose conclusions are
folded in below ([History](#history)).

Last updated 2026-10-04. Status per phase is in [Roadmap](#roadmap).

**Related docs**

| Doc | What it covers |
|---|---|
| [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md) | **how it all works today** (this file is the plan and history) |
| [`repo/brain/1.0.0/README.md`](../repo/brain/1.0.0/README.md) | brain as a service: workflows, `workflow.yaml` fields, bus actions |
| [`repo/brain/1.0.0/README_ENGINE.md`](../repo/brain/1.0.0/README_ENGINE.md) | `WorkflowEngine` internals, guards, termination paths |
| [`repo/brain/1.0.0/TODO.md`](../repo/brain/1.0.0/TODO.md) | brain's open engine questions |
| [`packages/agent_harness/README.md`](../../../packages/agent_harness/README.md) | the harness: skills, workflows, drivers, checks |
| [`docs/AGENT_HARNESS_COMPARISON.md`](../../../docs/AGENT_HARNESS_COMPARISON.md) (repo root) | how the harness was unified from two earlier apps |
| [`AGENTS.md`](../AGENTS.md) § Runtime-wide HALT | the stop signal every agent must obey |

---

## Terms

| Term | Meaning here |
|---|---|
| **mind** | Whatever decides the next action: an LLM call (brain's adapters) or a rented agent's loop |
| **runtime** | botgear-specific machinery around a mind: tool discovery, safety policy, HALT, conversation reflexes, logs |
| **rented harness** | An agent product whose loop we don't own: Claude Code today; Codex, ACP agents, the Claude Agent SDK next |
| **agent surface** | What botgear exposes to *any* mind: capabilities, their actions + schemas, state, HALT. Today only brain can reach it |

---

## Where things stood (before Phase 0)

The starting point the phases below were planned from. For how things work
now, see [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md).

### brain: a botgear-native runtime fused to an in-house agent loop

`repo/brain/1.0.0`, an in-process service. Two halves:

1. **Agent loop** (commodity): model adapters (Ollama, Anthropic, OpenAI,
   mock), the LLM → tool call → result loop, step/token/time budgets.
2. **botgear runtime** (the valuable half; nothing rented has it):
   - **Discovery:** the live tool catalog from every service's retained
     `/{type}/{id}/meta`; `describe_capability`; lazy disclosure above
     `lazy_tools_threshold` actions.
   - **Safety:** `allowed_tools.yaml` allow/block rules, `default_policy:
     allow` + the shared hardware excludes (`agent_surface/excludes.py`), and
     runtime-wide HALT (cancel every run, refuse new ones until resume).
   - **Conversation reflexes** (deterministic, no LLM): `exit_on_phrase`,
     `loop_on_timeout`, `speak_wait`, `echo_filter`, `resume_on_done`,
     `loop_breaker`, `stamp_time`, `message_window_size`.
   - **Observability:** run dirs (`steps`/`tool_calls`/`requests.jsonl`),
     live run phase on `/brain/{id}/state`, `get_run_log`, the brain panel.

The audio side it relies on lives in `packages/rlx_audio`: the STT echo
gate, the always-on stop-word spotter, `speak(wait=)`, markdown-free speech.

**Proven:** voice control end to end (mic → `stt_local` → `general_assistant`
on `qwen3:8b` → `tasmota`/`speech_local`), plus the earlier Discord path.

### agent_harness: rented agents, batch work, checked results

`packages/agent_harness`, a generic library (no botgear imports; synced to
other repos). "Own the workflow, context, tools, policies, state and
evaluation; rent the agent harness."

- A run = `WORKFLOW.md` contract + `SKILL.md` folders, executed by a driver
  (`claude_code`: Claude Code headless with tools; `prompt_inject`: one model
  call, no tools) in an isolated workspace.
- Files in, files out. External **checks** decide success; real-world effects
  happen only in `on_success`, after every check passes. Runs take minutes.
- Safety: residency gate (`local_only` default refuses cloud agents), network
  policy, declared secrets, write access only to `output/`.
- **No live tools:** the Claude Code driver runs with `--strict-mcp-config`
  and no MCP servers. Mid-run, an agent can only run the skill scripts it was
  handed. `Capabilities.mcp` exists but nothing uses it.
- In production in other cloudseeder apps. Not used by botgear.

### The gap

botgear has **no agent-facing interface**. brain reaches services through
internal bus plumbing; nothing outside brain (Claude Code, Codex, the Claude
desktop app, an agent_harness run) can discover a service, call it, read its
state or stop it. Every capability we add is usable by brain only.

---

## Direction

Split brain along the seam above. The **runtime** half becomes botgear's
agent surface, shared by every mind; the **agent loop** becomes one
interchangeable mind among several:

```
            minds                          agent surface                botgear
 ┌────────────────────────┐        ┌───────────────────────────┐   ┌──────────────┐
 │ brain (local LLM loop) │──lib──▶│ discovery · policy · HALT │──▶│ services on  │
 │ Claude Code / Codex /  │──MCP──▶│ invoke · state · audit    │   │ the bus      │
 │ agent_harness runs     │──MCP──▶│ (one policy for all)      │   │              │
 └────────────────────────┘        └───────────────────────────┘   └──────────────┘
```

Principles:

- **One policy, every mind.** Allow/block rules, shared excludes and HALT are
  enforced in the surface, not re-implemented per agent.
- **HALT outranks everything.** Any mind's calls are refused while halted;
  any running agent is cancelled by a halt.
- **The harness stays generic.** It gains MCP as a feature; it never imports
  botgear. MCP is the seam.
- **Real-time stays local and deterministic.** Reflexes (echo, stop words,
  turn-taking, exit phrases) never wait on a rented agent.
- **The surface's quality is the product.** Argument schemas, docstrings and
  capability-level actions decide how well *any* agent uses botgear.

---

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Agent surface library extracted from brain | **done** 2026-10-04 (`src/botgear/agent_surface/`) |
| 1 | botgear MCP gateway | **done** 2026-10-04 (`src/botgear/mcp_gateway/`; validated with Claude Code) |
| 2 | MCP support in agent_harness | **done** 2026-10-04 (`mcp:` in WORKFLOW.md; `agent_harness/` robot-health) |
| 3 | botgear `agent` service (harness runs on the bus) + brain `delegate` | **done** 2026-10-04 (`repo/agent/1.0.0/`); `robot-health` validated live; voice delegation not yet tried live |
| 4 | Pluggable mind in brain (rented agent session) | **done** 2026-10-04 (`claude_agent` backend); validated live by voice |
| ongoing | Capability contracts: schemas, docstrings, capability-level actions | schemas fixed on both transports 2026-10-04; rest open |

### Phase 0: extract the agent surface from brain

Move the botgear-runtime pieces out of `repo/brain/1.0.0/brain/` into one
module (working name `botgear/agent_surface`):

- tool catalog (meta subscription, tool naming, capability prefixes),
- policy (allow/block/default, shared excludes, HALT check),
- invocation (bus request/reply, today's `ToolExecutor`),
- audit (one record per call, today's `ToolCallRecord`).

brain uses it with no behavior change; its test suite is the regression net.
**Done when:** brain's tests pass unchanged and nothing botgear-specific is
left in brain outside the engine and conversation reflexes.

**Done (2026-10-04).** `src/botgear/agent_surface/`:

| Module | Moved from brain | Contents |
|---|---|---|
| `models.py` | `schemas.py` | `ToolDescriptor`, `ToolResult`, `ToolPattern`, `ToolPolicy` (brain's `AllowedTools`), `SafetyVerdict` (+ guard `halted`), `ToolCallRecord` |
| `policy.py` | `safety_gate.py`, `context_loader.py` | `topic_matches`, `capability_prefix`, `check_call`, `policy_from_dict` / `load_policy_file`; new `halt_verdict()` for callers that don't get the HALT broadcast (the gateway) |
| `excludes.py` | `workflows/_shared/excluded_tools.yaml` | `SHARED_EXCLUDES`, now a constant so it ships in every packaging |
| `catalog.py` | `service.py` | `descriptors_from_meta`, `add_meta`, `evict_proxy`, `released_proxy`, `watch_meta` / `watch_releases` (bus passed in) |
| `invoke.py` | `tool_executor.py`, `service.py` | `ToolExecutor`, `bus_reply_iter` |

brain re-exports every moved name from its old module (`brain.schemas`,
`brain.safety_gate`, `brain.tool_executor`), so its code and tests are
unchanged; `tests/test_agent_surface.py` pins the surface's API and checks
brain's names are the same objects. Brain does not call `halt_verdict` (it
gets the HALT broadcast and cancels its runs), so its behavior is identical.

### Phase 1: botgear MCP gateway (the keystone)

An MCP server in the backend, built on Phase 0:

- **Tools:** each allowed service action with its argument schema, plus
  `list_capabilities`, `describe_capability`, `get_state(topic)`,
  `wait_for(topic, condition, timeout)`, `halt`. Above a size threshold,
  brain's lazy pattern (describe before use).
- **Live catalog:** MCP `tools/list_changed` when services start, stop or
  are renamed.
- **Auth:** botgear's existing API Clients (`X-API-Key`), with a policy
  profile per client (e.g. switches yes, motion no).
- **Safety:** every call checked against HALT and the client's profile, and
  audit-logged.
- **Transport:** streamable HTTP in the backend; a small stdio shim for
  clients that only speak stdio.

**Done when:** Claude Code on a dev machine, with no brain involved, can say
what's on the network and turn switch one off, and a HALT stops it.

**Implemented (2026-10-04)** as `src/botgear/mcp_gateway/` (how to connect,
profiles, audit: its [README](../src/botgear/mcp_gateway/README.md)). Choices,
per the open decisions' recommendations:

- In the backend at `/v1/mcp`: streamable HTTP, **stateless** with JSON
  responses (auth every request; tool list recomputed per `tools/list`, so
  no `list_changed` push is needed). SDK: `mcp` 2.3 (protocol 2025-11-25 and
  2026-07-28).
- One tool per allowed action (`<service>__<action>`), annotation hints from
  the action name; built-ins `botgear_list_capabilities`, `botgear_get_state`,
  `botgear_halt`.
- Per-client profiles (`<data_dir>/mcp/profiles/<client>.yaml`), default:
  allow + shared excludes (so no video) + no brain self-control + no config
  writes. HALT refuses everything but `botgear_halt`.
- Audit: `<data_dir>/mcp/calls.jsonl` + `/mcp/gateway/calls`.

Deferred: `wait_for`, MCP resources/subscriptions, elicitation, rate limits,
a stdio shim (Claude Code speaks HTTP with headers directly).

### Phase 2: MCP in agent_harness (generic)

- `WORKFLOW.md` gains `mcp:` (servers + allowed tool patterns), gated by
  policy like `network` (denied unless declared).
- `claude_code` driver writes the workspace's `--mcp-config` and adds
  `mcp__<server>__<tool>` allowlists; events already normalize tool calls.
- Checks may verify the **physical world** (read state back through the
  botgear API) instead of trusting the agent's report.

**Done when:** a "robot health report" workflow gathers service states and
logs over MCP, writes a report, and its check verifies each claim against
live state. (First task to confirm; see [Decisions](#open-decisions).)

**Implemented (2026-10-04).**
- **Harness** (generic, `packages/agent_harness`): `WORKFLOW.md` `mcp:`
  servers (url, headers with `{secret.NAME}` refs, an explicit `tools`
  list, optional `ca_cert`); policy `mcp: deny` by default; agents without
  MCP refused; Claude Code gets an inline `--mcp-config` with `${NAME}`
  header refs (expanded by the CLI from the process env, verified live) and
  exact `mcp__<server>__<tool>` allowlists; checks receive the declared
  secrets and their output is redacted. Other apps' agent
  tests unchanged.
- **botgear** (now `repo/agent/1.0.0/`, see its README): the
  `robot-health` workflow (read-only tools only; facts.json + report.md),
  `checks/verify_facts.py` (re-reads the robot via the gateway, compares
  every claim), `run_robot_health.py` (requires `--cloud-ok`). botgear now
  depends on `agent_harness`.

### Phase 3: botgear `agent` service + brain `delegate`

- A service (sibling to brain, `/agent/{id}/control`) that runs harness
  workflows from the bus: start / cancel / status, events and live phase on
  `/agent/{id}/runs/{run}/...`, a UI panel like brain's, HALT cancels runs.
- brain gains a `delegate(workflow, inputs)` tool: a long, checked task
  ("investigate why the battery drained") runs on the agent service; brain
  speaks the result when it lands.

This is project (C) of the 2026-10-03 strategy, now with live tools via the
gateway.

**Implemented (2026-10-04)** as the in-process service `agent`
(`repo/agent/1.0.0/agent_runner.py`, README there):

- Owns botgear's harness content: bundled `content/` (moved from
  `apps/botgear/agent_harness/`, so it ships in frozen builds with the
  repo), per-instance workflows/skills/runs under `<data_dir>/agent/<id>/`.
- `list_workflows`, `start_run`, `cancel_run`, `get_run`, `wait_run`,
  `list_runs`, `get_run_log`; runs execute `run_workflow` in a worker thread;
  events stream to `runs/<run>/events` (redacted), status on retained
  `state`, results on retained `runs/<run>/result`.
- Config: `residency` (default `local_only`), `mcp_api_key`,
  `mcp_endpoint` / `ca_cert` (defaults: this backend), `announce_topic`
  (speak the outcome), `claude_cli`, concurrency + retention.
- HALT cancels runs and blocks new ones; the gateway's default profile
  blocks `start_run` / `cancel_run` (no agents launching agents).
- **Delegation from brain** needed no new brain code: `general_assistant`
  is default-allow, so the agent service's actions are already tools; its
  prompt now says to hand long checks to `start_run`, say it's started, and
  let the announcement report the result (`get_run` if asked earlier).
- UI panel: settings warnings, workflows with Run, live runs + events +
  Cancel, recent runs with checks, cost and the report.

**Validated live 2026-10-04:** `robot-health` started from the agent panel
(the bus `start_run` path) with `residency: cloud_ok` and an API-client
key: `completed`, 3/3 checks, `verify_facts.py` "OK: 26 claims and 9
services match the live robot". The agent used only the workflow's two
read-only MCP tools (`botgear_list_capabilities` ×1, `botgear_get_state`
×7). 34 s, **$0.47** — `model` unset, so Claude Code's default (Opus);
set the service's `model` for a cheaper agent. Its report was accurate
(no errors; e.g. the scanner alerts on events but has no `alert_target`).
Not yet exercised live: delegation by voice (`general_assistant` →
`start_run` → spoken result).

Found on the way: built-in services (agent, brain, clock, ...) had no
Configure button (their `config_schema` never reached the `service_meta`
row the UI reads), and a plain backend exit's config snapshot reverted
hand-edited ymls; both fixed.

### Phase 4: pluggable mind in brain

Keep brain's conversation runtime; make the mind selectable: today's
step adapters, or a rented agent session. **Implemented 2026-10-04** as
brain's `claude_agent` backend (details in brain's README, "`claude_agent`:
a rented session as the mind"). **Validated live 2026-10-04** with
`general_assistant` by voice (results below).

**Measured first** (2026-10-04, Claude Agent SDK 0.2.163, one persistent
session, in-process tools, scripted utterances; "→ reply" is time from the
utterance to the reply being issued):

| Mind | → reply | Cost |
|---|---|---|
| local `qwen3:8b` via ollama (brain's `general_assistant`, 11 real runs) | median 5.6 s, p90 19.9 s, max 27.2 s (heard → first speak) | $0 |
| Claude Haiku session, 3 tools | 1.4–2.5 s | $0.003–0.005 per warm turn, $0.025 first turn |
| Claude Haiku session, 89 tools (live catalog size) | 1.2–3.9 s | $0.005–0.009 per warm turn, $0.07 first turn |
| Claude Sonnet session, 3 tools | 1.5–6.6 s | $0.006–0.008 per warm turn, $0.05 first turn |
| `claude_agent` through brain's engine (Haiku, stub robot) | 1.0–8.8 s (first turn slowest) | $0.013–0.015 for a 3-utterance conversation |

So the working assumption ("a local model stays right for spoken
back-and-forth") was wrong on latency: a rented Haiku session answers
faster than the local model, at about half a cent a turn (≈ $0.10–0.20
for a 20-turn conversation). The local model keeps the $0 and
data-stays-home advantages, so it stays the default.

**Live voice runs** (2026-10-04, Haiku, `general_assistant`, real mic,
STT and TTS; "→ speech" is from the utterance being heard to the reply
starting to play):

| Turn kind | → speech | Cost per turn |
|---|---|---|
| plain reply ("hello", "no") | 2.3–3.7 s | $0.008–0.009 |
| first turn of a session | 12–13 s | $0.018 (more on a cold cache) |
| with tools (status check: 4 runtime tools; scanner: `list_devices`) | 9–13 s | $0.017–0.042 (a big tool result is the expensive part) |

A 5-utterance conversation cost $0.093. Exit phrase, HALT (Stop button)
and cancel each closed the session, with no Claude CLI process left
behind. Found and fixed on the way:

- **The session repeated itself.** Offered `speak`, it sometimes ended a
  turn on a speak with no text; Claude Code injected "no visible output"
  and the model spoke again (the time, 4 times, until the loop breaker
  tripped, $0.16). It also paraphrased its own speak in its closing text,
  so the operator heard two answers and each such turn cost 2–3 model
  calls. Fix: the session isn't offered the reply channel's `speak` (like
  `listen`); its closing text is the only reply.
- **STT heard "goodbye" as "GOOD BY"**, so the exit phrase never matched.
  The goodbye workflows now also match `good by`.
- First turns are slow, and the session sometimes calls the runtime's
  `get_time` instead of using the `[now: …]` stamp on the utterance;
  left as is for now.

**Design decisions:**

- **Tools in-process, not through the gateway.** The roadmap assumed the
  session would use the Phase 1 gateway. In-process tools route through
  brain's own gate, step log and HALT, need no API key or TLS, and keep
  one policy (the workflow's `allowed_tools.yaml`). The gateway stays for
  external agents.
- **The engine owns listening and speaking; the session's closing text is the reply.**
  A turn that ends with a tool call and no text makes Claude Code inject
  "your previous response had no visible output", and the model speaks
  again; the first live run looped like this until the loop breaker
  stopped it. Letting the closing text be the reply fixed it and saves a
  round trip per turn; since the live voice runs the session isn't
  offered the reply `speak` at all, so it can't happen again.
- **No lazy disclosure** for a session mind: 89 tools cost ~1 s and ~half
  a cent, against a discovery round trip per new capability.
- **Guards:** `claude_agent_max_usd_per_run` (the SDK's `max_budget_usd`),
  no built-in tools, user/project settings ignored, no session
  persistence.

**Not done:** ACP/Codex sessions (the same interface: `open` / `turn` /
`close`); per-turn cost on the brain panel (it's in `summary.json` and
`steps.jsonl`); a "fast reflex" path that answers simple commands
locally and escalates the rest.

### Ongoing: capability contracts

What every phase depends on:

- **Argument schemas come from type annotations — on both transports since
  2026-10-04.** `rlx_bus/introspect.py::args_schema_for` derives a method's
  JSON Schema from its signature (Pydantic `TypeAdapter`); a hand-written
  `@service_method(args_schema=...)` wins. Until 2026-10-04 only the
  in-process path used it: subprocess services' methods manifest
  (`SubprocessService._methods_payload`, the backend's only source for
  them) published just the hand-written schema, usually none, so tools
  like `mouth__speak` reached agents with empty schemas (found via the MCP
  gateway); and the in-process path ignored hand-written schemas. Both
  fixed (`tests/test_method_schemas.py`); `**kwargs` methods now advertise
  an open schema instead of "no arguments". So schema quality = annotation
  quality. Remaining: untyped params get loose schemas and nothing tracks
  that (`tests/test_service_schema_ratchet.py` covers published-topic
  schemas, not method arguments) — a ratchet for "every arg is typed" is
  the natural next step.
- **Docstrings** on every action (they become the tool description).
- **Capability-level actions** (`power_switch.power_set`) instead of
  per-device topics. Default-allow + shared excludes made capability-generic
  *consumption* possible in brain; contracts make it reliable for any mind.

---

## Open decisions

1. **Gateway placement:** inside the backend (recommended) or a separate
   process?
2. **Tool granularity:** one MCP tool per action (recommended, with lazy
   fallback above a threshold) or a few generic `describe`/`call` tools?
3. **Data residency for robot data:** may camera frames and audio go to cloud
   agents? Recommended: per-client, opt-in, mirroring the harness's
   `local_only` default.
4. **First proving task** for Phases 2-3 (a robot health report, a device
   inventory, something used weekly).

---

## History

**2026-10-03, `AGENT_SERVICE_STRATEGY.md`** reviewed agent_harness vs brain
for "a better brain" and split the ask into three projects:

| Project | Then | Now |
|---|---|---|
| (A) mic → switch end to end | "nearly done in brain, a transport swap" | **Done** (2026-10-03/04): `general_assistant` voice loop, plus the STT echo gate, stop words, HALT, `speak_wait`, `resume_on_done`, loop breaker found along the way |
| (B) better brain: capability-generic consumption; `args_schema` beyond tasmota | open | Both **done**: `default_policy: allow` + shared excludes + lazy disclosure; signature-derived `args_schema` on both transports (subprocess path fixed 2026-10-04). Typed-args coverage → [Ongoing](#ongoing-capability-contracts) |
| (C) agent_harness for minutes-scale, checked tasks, dispatched from brain | needs a first task | → [Phase 3](#phase-3-botgear-agent-service--brain-delegate), now preceded by the gateway |

What that review didn't consider is the opposite direction, external agents
driving botgear, which is why the gateway (Phase 1) is now the keystone.
Its other findings still hold: agent_harness is the wrong shape for a live
voice loop, and brain's reflexes would have to be reinvented by any
replacement, so they stay.

**2026-10-04, Phase 4 live.** `claude_agent` validated by voice with
`general_assistant`: one reply per utterance after two fixes (no reply
`speak` for the session; `good by` exit phrase), exit phrase / HALT /
cancel all close the session, $0.008–0.04 per turn. Measurements in
[Phase 4](#phase-4-pluggable-mind-in-brain).

**2026-10-04, Phase 3 live.** `robot-health` from the agent panel:
completed, checks passed against the live robot, $0.47 on Opus. Measurements
in [Phase 3](#phase-3-botgear-agent-service--brain-delegate).
