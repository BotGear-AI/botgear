# rlx_decision

Fast "System One" decision client for botgear: typed, non-generative
decisions (yes/no, pick-one, rate-on-a-scale) answered in well under a
second, so simple commands ("turn the light off", "look up") can act
without waiting seconds for an LLM turn.

Backed by TypeSafe AI's **Jev** models (live, cloud) or a deterministic
regex-heuristic **mock** (offline, default). The LLM ("System Two" —
brain's model adapter) stays in charge of anything open-ended; this
package only answers bounded questions with a confidence attached.

```bash
pip install -e packages/rlx_decision     # deps: pydantic>=2, requests
```

## Primitives

A call evaluates one **state** (any JSON-able value — a string utterance
or a dict) against a dict of named **questions**, all answered at once.

| Question | Fields | Answer | Use for |
| :--- | :--- | :--- | :--- |
| `Noul` | `instructions` | `NoulAnswer.noul` — probability 0..1 (0.5 = undecided; no separate confidence) | intent gates, safety checks, exit phrases |
| `Choice` | `instructions`, `options` (1–255) | `ChoiceAnswer.choice`, `.probabilities`, `.confidence` | target device, tool, pose, canned reply |
| `Score` | `instructions`, `levels` (2–10, ordered) | `ScoreAnswer.score` (1..n), `.probabilities`, `.confidence` | urgency, risk, priority |

```python
from rlx_decision import Choice, Noul, create_decision_client

client = create_decision_client()          # offline mock (no key given)
resp = await client.system_one(
    "turn the light off",
    {
        "is_direct": Noul(instructions="Is this an unambiguous direct command to change the power state of a light or switch?"),
        "target_state": Choice(instructions="What is the requested target state?", options=["on", "off", "toggle", "none"]),
    },
)
resp.answers["is_direct"].noul            # 0.99
resp.answers["target_state"].choice       # "off"  (confidence 0.98)
resp.latency_ms                           # set by the mock; None from the live API
```

Single-question shortcuts: `decide_noul(state, instructions) -> float`,
`decide_choice(state, instructions, options) -> ChoiceAnswer`,
`decide_score(state, instructions, levels) -> ScoreAnswer`.

## Modes

`DecisionClient(api_key=None, base_url=None, timeout_seconds=3.0, mock=None)`

- **Mock** — the default: chosen when `mock=True`, or when `mock` is
  omitted and no `api_key` argument is given (or it starts with `mock`).
  `TYPESAFE_API_KEY` in the environment does **not** make a default client
  live, so code that builds `DecisionClient()` never sends state off-host. `MockDecisionClient`
  answers from regex heuristics for the scenarios botgear exercises:
  light/switch power, pan-tilt gaze presets, emergency halt, exit intent,
  "and tell me what you see" follow-ups, on/off/toggle, keyword overlap
  between the utterance and option names for device/tool picks, and a
  danger/caution keyword rubric for `Score`. Tests can override any
  question id with `register_handler(question_id, fn(state, question))`.
- **Live** — needs an explicit opt-in: an `api_key` argument, or
  `mock=False` (which then reads `TYPESAFE_API_KEY`).
  `POST {base_url}/v1/system-one` with `Authorization: Bearer
  <key>` and `{"state": ..., "questions": {id: question.model_dump()}}`;
  `base_url` defaults to `https://api.typesafe.ai` (or
  `TYPESAFE_BASE_URL`). The request/response shape is our reading of the
  Jev API and **has not been run against a real key yet**.
- **Live → mock fallback** — any HTTP error or exception logs a warning and
  answers *that call* from the mock; the next call tries the API again. Note this means a cloud outage silently
  degrades to heuristics that report high confidence.

## Designing questions

- **State** should be the facts under evaluation, not a chat prompt. Three
  shapes cover botgear: a dialogue/utterance (`{"utterance": ..., "speaker",
  "channel"}` or just the string), a tool-candidate list (`{"query": ...,
  "available_tools": [{name, description}]}`), and a sensor reading
  (`{"sensor": "sonar_front", "distance_cm": 14.2, ...}`). The mock reads
  text from `utterance` / `text` / `query` / `message` /
  `latest_utterance` (also nested under `dialogue`).
- **Questions** must be objective and bounded. "Is the user explicitly
  expressing an intent to end the conversation (goodbye, stop talking,
  dismiss)?" — not "Does the user seem finished?".
- **Always give a `Choice` an escape hatch** (`"none"` /
  `"none_of_the_above"`) so "not a reflex" is an answer rather than a
  forced wrong pick.

### Confidence policy

Callers act on a decision only above a threshold, and otherwise hand the
request to the LLM unchanged:

```
0.0            0.70            0.95          1.0
├── escalate ───┼── clarify ─────┼── execute ──┤
   System Two     ask / confirm     act now
```

Implemented today: **execute** at `confidence >= 0.95` (configurable) with
the `Noul` gate at `>= 0.90`; everything else escalates. The middle
**clarify** zone ("the workbench light or the office light?") is not built
— those cases currently go to the LLM.

## How botgear uses it

| Where | What |
| :--- | :--- |
| `repo/brain/1.0.0/brain/reflex_engine.py` | `FastReflexEngine`: before each model call, evaluates the last *heard* utterance (never the workflow prompt; each utterance once). Finds `power_set`/`power_toggle` and `servo_write` tools plus a `speak` tool in the live catalog; on a confident hit returns the actuation + a spoken ack (`interrupt: true`) as the turn's actions, skipping the LLM. |
| `brain/workflow_engine.py`, `schemas.py` | Opt-in per workflow with `fast_reflex: true` (default off). `conversation_session` opts in. |
| `repo/fast_decision/1.0.0` | In-process bus service on `/fast_decision/<id>/control`: `evaluate`, `decide_noul`, `decide_choice`, `decide_score`, plus `evaluate_lighting` / `evaluate_pan_tilt` (decide only — they don't actuate). Config: `api_key` (encrypted), `base_url`, `confidence_threshold`, `residency`, `timeout_seconds`. |
| `src/botgear/agent_surface/router.py` | `FastToolRouter.route_tools(query, catalog)`: narrows a large catalog to the top-k tools via one `Choice` over `topic::action` keys (first 250). |
| `tests/test_fast_decision.py`, `tests/test_brain_conversation.py` | Primitives, service + `on_start`, reflex lighting/pan-tilt, power_set-over-toggle, single-light-needs-no-name, router, and the engine path with `fast_reflex` on/off. |
| `tools/bench_worke_lighting.py` | Latency bench for the light/gaze reflexes vs. a *simulated* (sleep-based) LLM turn; `--live` uses the real API. |

Reflex behaviour worth knowing:

- **Lights.** A device advertising both `power_set` and `power_toggle`
  uses `power_set`, so "turn off" can't toggle an off light on. A
  toggle-only device only reflexes on "toggle"; on/off goes to the LLM.
  With exactly one light in the catalog the utterance needn't name it.
- **Pan-tilt.** Presets map to fixed angles tuned for worke's head — tilt
  90 level / 120 up / 60 down, pan 90 center / 135 left / 45 right — on
  servos whose topic id contains `tilt`/`head` and `pan`.
  `requires_llm_followup` is computed ("look up *and tell me what you
  see*") but nothing acts on it yet.

## Safety, residency, HALT

- **Secrets** — the service's `api_key` is a `SecretStr`, stored with
  botgear's Fernet config encryption (`Encrypted--...`).
- **Residency** — the service defaults to `residency: local_only`, which
  forces the mock so no utterance or telemetry leaves the host;
  `cloud_ok` allows the live API.
- **HALT** — the service answers "halted"/zero-confidence while the runtime
  is halted, and brain cancels every run on HALT, so reflexes can't fire.

## Not built yet

- Brain's reflex engine and `FastToolRouter` build a default client, so
  they always run on the offline mock; using live Jev there means wiring
  in the `fast_decision` service's key/`residency` (or injecting a client).
- `FastToolRouter` is tested but not wired into the agent surface's
  catalog / `lazy_tools` path.
- Clarify zone (above); acting on `requires_llm_followup` to overlap a
  gaze move with an LLM vision turn.
- Pre-flight safety gate (`Noul` hazard / operating-envelope checks before
  any proposed tool runs) and semantic filtering of high-rate bus streams
  (sensor urgency `Score`, "does this need the robot?" `Noul`).
- Client internals: decision cache (`DecisionResponse.cached` is unused),
  connection pooling / retries, an on-device classifier fallback for
  `local_only` beyond the regex mock, `latency_ms` on live answers.
- Live validation: get a TypeSafe key, confirm the API contract above, and
  bench a real Tasmota switch on worke (Ollama 8B vs. reflex).
