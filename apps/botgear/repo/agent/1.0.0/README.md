# agent — checked agent work on the live robot

In-process botgear service that runs [`packages/agent_harness`](../../../../../packages/agent_harness/README.md)
workflows from the bus. A rented agent (Claude Code) does minutes-scale work
on the robot, reaching it only through botgear's MCP gateway
([`src/botgear/mcp_gateway`](../../../src/botgear/mcp_gateway/README.md)), and
every workflow's checks verify what it reports against the robot before a
run counts as `completed`. Phase 3 of [`docs/AGENT_ROADMAP.md`](../../../docs/AGENT_ROADMAP.md);
how it fits with brain and the gateway: [`docs/AGENT_ARCHITECTURE.md`](../../../docs/AGENT_ARCHITECTURE.md).

```
type: agent · language: builtin (in-process) · module: agent_runner.AgentService
```

| | brain | agent |
|---|---|---|
| Mind | in-house loop, local model (Ollama) by default | rented agent (Claude Code) |
| Pace | real-time conversation, a tool call per turn | one run, minutes |
| Result | live side effects | files (`report.md`, …) whose claims checks verified |

They work together: brain's `general_assistant` hands long checks to this
service (`start_run`), and the result is spoken when the run ends
(`announce_topic`).

## Layout

```
repo/agent/1.0.0/
  agent_runner.py            the service
  content/                   bundled workflows (read-only)
    AGENT_CONTEXT.md         the agent's run context (workspace AGENTS.md)
    workflows/robot-health/  WORKFLOW.md + checks/verify_facts.py
    skills/
  run_robot_health.py        the same workflow from the command line
  ui/                        the panel (View.tsx → dist/ui.js)
<data_dir>/agent/<id>/
  workflows/  skills/        per-instance, shadow the bundled ones by name
  AGENT_CONTEXT.md           optional override
  runs/<ts>-<workflow>-<agent>-<run_id>/   task.md, events.jsonl, checks.json, result.json, output/
```

## Setup

1. Add the service (Catalog → **agent**), then open its config:
   - `mcp_api_key`: an API client's secret (Tools → API Clients). Runs use it
     to reach the robot; that client's gateway profile applies.
   - `residency`: `cloud_ok` to let Claude Code run. It is `local_only` by
     default, which refuses agents that send the robot's state off-host.
   - `announce_topic`: e.g. `/speech/mouth/control` to hear when runs finish.
   - `claude_cli`: path to `claude` if the backend's PATH doesn't have it.
2. The backend's `ssl/cert.pem` and `https://localhost:<port>/v1/mcp` are used
   by default (`ca_cert`, `mcp_endpoint` to override).

## Bus actions (`/agent/<id>/control`)

```
list_workflows()                                    → [{name, description, inputs (auto?), outputs, mcp}]
start_run(workflow, inputs={}, agent=None, model=None)   → {run_id, status: running}
cancel_run(run_id)                                  → {cancelled}
get_run(run_id)                                     → live status, or the final summary + deliverable text
wait_run(run_id, timeout_seconds=20)                → get_run after the run ends or the timeout (max 25 s)
list_runs(limit=20)                                 → active, then recent runs
get_run_log(run_id, limit=200)                      → {events, checks} from the run's workspace
```

Inputs named `endpoint` / `ca_cert` are filled from the config when left
out; a workflow declaring the secret `BOTGEAR_MCP_KEY` gets `mcp_api_key`.
Errors come back as `{error}` (unknown workflow, too many runs, key not set,
HALTed).

Topics: `state` (retained: settings summary, active runs with live phase),
`runs/<run_id>/events` (each agent event, secrets redacted),
`runs/<run_id>/result` (retained: status, checks, cost, deliverables).

## Safety

- **Residency:** cloud agents need `residency: cloud_ok`, set by the operator.
- **HALT:** cancels every run (the agent process is stopped); `start_run`
  refuses while halted.
- **Least privilege:** a workflow lists exactly the MCP tools its agent may
  use (`robot-health`: two read-only ones); the API client's gateway profile
  applies on top. The gateway's default profile blocks `start_run` /
  `cancel_run` here, so an external agent can't launch more agents.
- **Checks decide:** a run is `completed` only when its checks pass on the
  live robot; `failed_checks` otherwise, and nothing is announced as done.

## robot-health

"What's running on the robot, and does anything look wrong?", with every
fact checked. The agent may only call `botgear_list_capabilities` and
`botgear_get_state`; it writes `output/facts.json` (halted, every service,
≥5 claims copied from live state) and `output/report.md`.
`checks/verify_facts.py` re-reads the robot with the same key and fails the
run on any mismatch.

From the command line (backend up, from `apps/botgear`):

```bash
BOTGEAR_MCP_KEY=<api-client secret> .venv/bin/python repo/agent/1.0.0/run_robot_health.py --cloud-ok
```

## Adding a workflow

Drop `<name>/WORKFLOW.md` (+ `checks/`) into `<data_dir>/agent/<id>/workflows/`
(or `content/workflows/` to bundle it). Follow the harness README ("Using it
in an app", "Live systems over MCP"): give the agent only the MCP tools the
task needs, read-only unless it must act; put arithmetic in skill scripts;
and make a check verify the robot's real state, not the agent's report.
