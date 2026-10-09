# Brain Gap Fixes — voice/chat-triggered device control

> **Update 2026-10-04:** the live end-to-end goal is met, by voice as well as
> Discord (`general_assistant`: mic → `stt_local` → brain → `tasmota` /
> `speech_local`). Capability-generic device use landed as
> `default_policy: allow` + shared excludes. Gap 6's generalization is done
> too: `rlx_bus/introspect.py` derives `args_schema` from method signatures
> for every service, on both transports (the subprocess manifest only
> started using it on 2026-10-04). Follow-ups:
> [`AGENT_ROADMAP.md`](AGENT_ROADMAP.md) § Ongoing: capability contracts.

Status: **Gaps 1-5 implemented, tested (1087 tests passing on
worke.local), deployed.** Live end-to-end test in progress. Triggered
by a concrete goal: DM workE on
Discord "Hi WorkE, turn on the office-cord" and have `brain` actually
call `tasmota`'s `power_set` on the `office-cord` proxy. Investigating
that goal surfaced gaps in `brain`'s tool-catalog/safety-gate machinery
that apply generally, not just to this one device.

Context: `brain` is already backend-agnostic at the config layer —
`mock`/`ollama`/`anthropic`/`openai` are resolved by name in
`brain/service.py::_adapter()`, each behind the same `ModelAdapter`
interface. Ollama is only in use today because no Anthropic/OpenAI key
is configured on `worke`. That part of the design already works;
nothing below changes it.

**Explicit decision — no `requires_human_approval`.** `brain` supports
pausing every tool call for an operator `approve()`, but gating every
concrete robot action behind a human click isn't viable as a standing
posture — it defeats the point of a voice-controllable robot. Not using
it here. Safety instead comes from `allowed_tools.yaml`'s default-deny
allow-list (scoped to exact, named devices — see Gap 2) plus workE's
existing `allowed_sender_ids` filter (only one Discord account can reach
the conversation at all).

---

## Gap 1 — tool-name collisions produce `tool_003`/`tool_007`

**Where:** `brain/adapters/{ollama,anthropic,openai}.py`, each with
their own near-duplicate `_encode_tools()`.

**What's wrong:** Function-calling APIs constrain tool names to
`^[a-zA-Z][a-zA-Z0-9_-]{0,63}$` — the brain's natural tool identity,
`<topic>::<action>`, doesn't fit. Current handling: use the bare action
name if it's valid and not already used by an earlier tool in the same
request; otherwise fall back to `tool_{i:03d}`. With three `tasmota`
proxies (`office-cord`, `sonoff-cord`, `sonoff-office-light`) all
exposing identical actions (`power_set`, `power_toggle`, `pulse`,
`list_relays`), only the first-iterated instance keeps `power_set` —
the other two become `tool_003`, `tool_007`, etc., distinguishable only
if the model correctly parses a `[topic: ..., action: ...]` string
appended to a possibly-empty description. This is a general problem
(any capability with 2+ live instances), not tasmota-specific, and
non-deterministic besides (iteration order isn't guaranteed stable
across runs).

**Also found:** `openai.py`'s `_encode_tools` doesn't even attempt the
bare-name path — *every* tool becomes `tool_NNN` for the OpenAI
backend, always. Worse than the other two adapters, and works against
"backend-agnostic" — a workflow prompt written as "call `listen`" only
holds for ollama/anthropic today, silently breaks for openai.

**Fix:** Factor one shared `resolve_tool_names(tools)` into
`brain/adapters/base.py`, used identically by all three adapters:
1. Bare action name, if valid + unique in this tool list (unchanged —
   every existing workflow prompt that says "call `listen`"/"call
   `speak`"/"call `write_memory`" keeps working as long as that action
   is live exactly once, the common case).
2. On collision or invalid characters: `<proxy_id>_<action>`, sanitized
   (non-`[a-zA-Z0-9_-]` → `_`, prefixed if it wouldn't start with a
   letter, truncated to 64 chars). `proxy_id` is read off the tool's
   topic (the segment before `control`) — e.g. `office-cord_power_set`,
   `sonoff-cord_power_set`. Legible, stable, and the model can match it
   directly against what the operator said without parsing anything.
3. `tool_NNN` kept only as a final, should-never-hit fallback if even
   the qualified name collides.

---

## Gap 2 — no allow-list entry for `/tasmota/*/control`

**Where:** `repo/brain/1.0.0/workflows/conversation_session/allowed_tools.yaml`.

**What's wrong:** `check_tool_call` is default-deny — nothing today
permits any `tasmota` action, so the model calling `power_set` gets
rejected by the safety gate regardless of tool-catalog visibility.

**Fix:** Add exact, per-device rules (not a `/tasmota/*/control`
type-wide wildcard) — least-privilege: adding a fourth Tasmota device
later doesn't silently become chat-controllable.

```yaml
- topic: /tasmota/office-cord/control
  actions: [power_set, power_toggle, list_relays]
- topic: /tasmota/sonoff-cord/control
  actions: [power_set, power_toggle, list_relays]
- topic: /tasmota/sonoff-office-light/control
  actions: [power_set, power_toggle, list_relays]
```

`list_relays` (read-only) included so the model can check current state
before acting instead of guessing. `pulse`/`connect`/`disconnect`
deliberately left out — momentary-actuator and device-reconfiguration
actions, different risk shape, not needed for "turn on/off."

---

## Gap 3 — `power_set`/`power_toggle` have no docstrings

**Where:** `repo/tasmota/1.0.0/src/tasmota_service/service.py`.

**What's wrong:** `ToolDescriptor.description` reads a method's
docstring (`m.get("doc")`). Empty today, so post-Gap-1-fix the model
still only has the qualified tool *name* to go on, no method
description at all.

**Fix:** Add one-line docstrings to `m_power_set`/`m_power_toggle`.

---

## Gap 4 — `conversation_session` doesn't know device control is in scope

**Where:** `repo/brain/1.0.0/workflows/conversation_session/prompt.md`.

**What's wrong:** The prompt is pure listen/speak/head-servo. Nothing
tells the model it may act on named switches, or to confirm back via
`speak()` after doing so.

**Fix:** Add a short instruction block naming the available switches
and when/how to use them, alongside the existing turn loop.

---

## Gap 5 — the engine only executes the FIRST tool call in a multi-call response

**Where:** `brain/adapters/{ollama,anthropic,openai}.py::_parse_response`
+ `brain/workflow_engine.py::_loop` + `brain/schemas.py::ModelResponse`.

**Found live, not by inspection** — the first real test of Gaps 1-4:
"turn on sonoff-cord" appeared to work, "turn off sonoff-cord" got no
response at all. Actual trace: the model returned `power_set` **and**
`speak(...)` in the same response. Every adapter's parser took only
`tool_calls[0]` (a documented v1 limitation — "one tool call per
step... future extension") and silently dropped the `speak`
confirmation. On the next turn the model, having already composed that
reply internally, emitted it as plain text instead of re-calling
`speak()` — a tool-call-less response parses as terminal `done`, which
`conversation_session` forbids, so the run crashed. The next message
landed on a dead run.

Separately surfaced by the same test: `office-cord` and `sonoff-cord`
are configured against the same IP — the model actually called the
*wrong* proxy (`office-cord`, since that's whichever proxy happened to
get the bare `power_set` name), and it only looked correct because
both names control the same physical device. Not a code bug, but a
config collision in the operator's existing Tasmota setup worth fixing
separately since it defeats per-device targeting.

**Fix (the real fix, not a prompt workaround — model rule-compliance
already proved unreliable twice in testing):**
- `ModelResponse` gains `actions: List[ProposedAction]`, always
  populated — a `model_validator` defaults it to `[action]` when only
  the singular field is set, so every pre-existing single-action path
  needed zero changes.
- All three adapters now parse *every* entry in
  `tool_calls[]`/`content[]` into its own `ProposedAction`, dropping
  only individual entries whose name is unknown (not the whole batch).
- `workflow_engine._loop` executes every action in a response's batch
  in order, sharing one `steps_used` increment (it counts LLM
  round-trips, not individual tool calls). A terminal outcome (done,
  disallowed, approval denied, `terminate_on` satisfied) anywhere in
  the batch still ends the run immediately, exactly as before — just
  checked per-action now instead of assuming one action per step.
- Regression tests added at both layers: `_parse_response` returns all
  actions in order (and drops only a genuinely-unknown one), and a
  full `WorkflowEngine` run confirms a 2-action batched response
  dispatches both tool calls and consumes exactly one step.

---

## Gap 6 — subprocess service methods carry no argument schema

**Where:** `packages/rlx_bus/src/rlx_bus/methods.py`,
`packages/rlx_bus/src/rlx_bus/service.py`,
`repo/tasmota/1.0.0/src/tasmota_service/service.py`.

**Found live, during Gap 5's retest.** With Gap 5 fixed, "turn off
sonoff-cord" correctly targeted the right device but failed:
`TasmotaService.m_power_set() got an unexpected keyword argument
'topic'`. Root cause: `power_set`'s `args_schema` was `{}` in every
tool the model ever saw — subprocess services (tasmota, arduino,
video, raspi, oakd, ...) get no type-hint-derived schema at all. The
one place that does derive one (`types_index.py::introspect_methods`)
needs to `import` the live class, which is impossible cross-venv for a
subprocess. With zero schema to go on, the model both guessed a wrong
`relay` default in an earlier attempt and hallucinated a phantom
`topic` argument here.

**Fix (narrow stopgap — full cross-venv introspection is a bigger job,
deliberately deferred):** `@service_method` gains an optional
`args_schema` kwarg. `tasmota`'s `power_set`/`power_toggle` now declare
real schemas (`additionalProperties: false`, which also directly
blocks the phantom-argument failure mode).

**Two propagation bugs found and fixed along the way** — the kwarg
alone didn't reach the wire:
1. `rlx_bus.methods.collect_methods()` rebuilds a fresh `MethodInfo`
   per bound instance from the decorator tag and only copied
   `name`/`doc`/`attr`/`publishes`/`publish_return` — silently
   dropping `args_schema`. This is the one that mattered: every
   subprocess service's self-published `/<type>/<id>/meta` methods
   list goes through this path, and that's what brain's tool catalog
   actually reads.
2. `botgear.framework.adapters.subprocess`'s own separate `MethodInfo`
   reconstruction (backing the `GET /v1/service-proxy/{id}/methods`
   REST endpoint) had the identical gap independently.

Verified live end-to-end: `sonoff-cord`'s `/tasmota/sonoff-cord/meta`
now carries the real schema, confirmed via a direct bus subscription
after restarting just the tasmota subprocess (no backend restart
needed — the subprocess self-publishes its own `/meta`).

---

## Sequencing

1. Gap 1 (tool naming) — self-contained, no open decisions, implement
   first. ✅
2. Gap 3 (docstrings) — self-contained, implement alongside Gap 1. ✅
3. Gap 2 (allow-list) + Gap 4 (prompt) — implement once device/action
   scope is confirmed (asked separately, not assumed). ✅
4. Gap 5 (multi-tool-call execution) — found during the first live
   test of 1-4; fixed. ✅
5. Gap 6 (subprocess arg schemas) — found during the first live test
   of the Gap 5 fix; narrow stopgap fixed for tasmota. ✅

## Decisions made

- Device/action scope for Gap 2: originally all three switches
  (`office-cord`/`sonoff-cord`/`sonoff-office-light`); simplified down
  to `sonoff-cord` only after the operator removed the other two
  proxies to reduce system complexity while testing. Re-add their
  allow-list rules (same shape) if/when those proxies come back.
- Model/backend: staying on local `ollama` (`qwen2.5:7b-instruct`) —
  operator's choice, aware of the reliability tradeoff already observed
  in testing (see Gap 5's discovery).
- Gap 5's multi-tool-call issue: fixed at the engine level (not a
  prompt-only workaround), given model rule-compliance had already
  proven unreliable twice before this was even found.
- Gap 6's schema gap: narrow stopgap (hand-written schema on tasmota's
  two switch methods) rather than building full cross-venv
  introspection for every subprocess service — that stays a separate,
  larger task for later.

## Still open (not yet actioned)

- The `office-cord`/`sonoff-cord` duplicate-IP config that masked real
  device mis-targeting during Gap 5's live test is now moot — the
  `office-cord` proxy was removed (operator, alongside
  `sonoff-office-light`, to reduce system complexity while testing).
- Gap 6 is a narrow, tasmota-only stopgap by design. Real cross-venv
  argument-schema introspection for every subprocess service (arduino,
  video, raspi, oakd, ...) is a separate, larger task, not yet started.
