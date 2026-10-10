# agent_harness

Run filesystem-defined agent work on **rented agent harnesses** (Claude Code
today; Codex and ACP agents next) instead of a home-grown agent loop.

> Own the workflow, context, tools, policies, state, and evaluation. Rent the agent harness.

The unit of work is either a `WORKFLOW.md` contract that uses Agent Skills
(`SKILL.md` folders), or a single skill run as the whole task. Every run
gets an isolated workspace, a policy gate, a normalized event stream, and
external checks that decide success. The agent's own opinion never does.

Generic by design: no application imports and no application or customer
names (`tests/test_no_app_imports.py`), because `packages/` is synced into
several downstream repos.

## The two concepts

- **Skill** (`SKILL.md` plus scripts) says *how* to do something: a procedure
  and the scripts that read the data.
- **Workflow** (`WORKFLOW.md`) is the contract. It declares:
  - **inputs, skills and outputs:** what goes in, which skills to use, and
    which files must come out;
  - **checks:** scripts that decide whether the run succeeded, not the
    agent's opinion;
  - **policy:** network access, time limit, turn limit;
  - optionally **stages** (ordered sub-steps) and **context_commands**, which
    pre-run a skill's scripts for agents that can't use tools.

An **agent** (driver) does the work: `claude_code` (tools; runs the skills'
scripts itself) or `prompt_inject` (one model call, no tools). The same
workflow runs on either. Details: [How it works](#how-it-works).

## Examples

Two runnable examples live in [`examples/`](examples/): **word-report** (the
basics: a skill, a workflow, a check) and **budget-check** (plain-value input,
a checked decision, deliverables, a write-back, and the tool-less path).

## Example: word-report

A complete, runnable example lives in [`examples/`](examples/) (and runs in the
test suite, `tests/test_example.py`). The job: report how long each document
in a folder is, with counts you can trust.

```
examples/
  docs/fox.txt, docs/hello.txt                 the input folder
  skills/count-words/                          a SKILL: how to count words
    SKILL.md
    scripts/count_words.py                     prints {"fox.txt": 9, "hello.txt": 2}
  workflows/word-report/                       a WORKFLOW: what to produce, and how it's judged
    WORKFLOW.md
    checks/check_counts.py                     recounts independently; must agree
  run_example.py
```

The **skill** says how to do the job (and brings the script that does it):

```markdown
---
name: count-words
description: Count the words in each text file of a folder. Use when a task needs exact word counts.
---
Run `scripts/count_words.py <folder>`. It prints JSON {"<file>": <count>}. Use these numbers exactly.
```

The **workflow** says what to produce from which input, with which skill, and
what counts as success:

```markdown
---
name: word-report
description: Report how long each document in a folder is.
inputs:
  docs: {type: path, required: true}
skills: [count-words]
outputs: [output/counts.json, output/report.md]
checks:
  - "{python} {workflow_dir}/checks/check_counts.py --docs inputs/docs --counts output/counts.json"
policy: {network: deny, max_minutes: 5}
---
Use the `count-words` skill on `inputs/docs`. Write `output/counts.json` exactly as the
script prints it, then `output/report.md`: one line per document, longest first.
```

Running it:

```python
from agent_harness import run_workflow

result = run_workflow("word-report", "claude_code", {"docs": "examples/docs"},
                      workspace_root="examples/runs",
                      workflow_paths=["examples/workflows"], skill_paths=["examples/skills"],
                      policy={"residency": "cloud_ok"},
                      on_success=lambda r: print("save", r.outputs))   # only if the check passed
print(result.status, result.checks_passed, result.checks_total)       # completed 3 3
```

What happens: the harness builds a fresh workspace with `inputs/docs`
(read-only) and a copy of the skill; Claude Code reads `task.md`, runs
`count_words.py`, writes the two files, and (because the workflow has a check)
runs `check_counts.py` itself before finishing. Then the harness runs the check
again on its own, and only that verdict counts. If the agent had written
`"hello.txt": 3`, the run would end `failed_checks` with
`FAIL: counts.json {...} != actual {...}`, and `on_success` would never run.

The same skill without a workflow (`run_skill("count-words", ...)`) also
works: the agent writes `output/OUTPUT.md`, but nothing checks the numbers.
That is the difference between the two in one line: **a skill is how; a
workflow is what, plus proof.**

Try it: `python examples/run_example.py` (needs the `claude` CLI, logged in or
with `ANTHROPIC_API_KEY`).

## Example: budget-check

A second example ([`examples/workflows/budget-check`](examples/workflows/budget-check),
tested in `tests/test_example_budget.py`) adds four things the first one doesn't
show: a **plain-value input**, a **decision** that gets checked (not just
numbers), **deliverables**, and a **write-back**. The job: did this month's
spending stay within budget?

```
examples/
  expenses/expenses.csv                        date,category,amount (totals 369.99)
  skills/sum-by-category/                      prints {"by_category": {...}, "total": 369.99}
  workflows/budget-check/
    WORKFLOW.md
    checks/check_verdict.py                    recomputes the total AND the verdict
  run_budget_check.py
```

```markdown
---
name: budget-check
description: Decide whether a month's expenses stayed within a budget, and say why.
inputs:
  expenses: {type: path, required: true}     # a file, mounted read-only at inputs/expenses
  budget: {type: string, required: true}     # a plain value, substituted as {input.budget}
skills: [sum-by-category]
outputs: [output/totals.json, output/verdict.md]
deliverables: [output/verdict.md]            # what a person reads; totals.json is the evidence
checks:
  - "{python} {workflow_dir}/checks/check_verdict.py --expenses inputs/expenses --budget {input.budget} --totals output/totals.json --verdict output/verdict.md"
context_commands:                            # only for agents without tools (prompt_inject)
  - title: Totals (sum_by_category.py)
    command: "{python} .claude/skills/sum-by-category/scripts/sum_by_category.py inputs/expenses"
---
Use the `sum-by-category` skill, compare the total with the budget ({input.budget}).
Write output/totals.json as the script prints it, then output/verdict.md whose first
line is exactly OVER BUDGET or WITHIN BUDGET, plus one short paragraph.
```

```python
def save_verdict(result):                    # the write-back: runs ONLY if the check passed
    return (Path(result.workspace) / "output/verdict.md").read_text().splitlines()[0]

result = run_workflow("budget-check", "claude_code",
                      {"expenses": "examples/expenses/expenses.csv", "budget": "300"},
                      workspace_root="examples/runs",
                      workflow_paths=["examples/workflows"], skill_paths=["examples/skills"],
                      policy={"residency": "cloud_ok"}, on_success=save_verdict)
result.status, result.handled, result.deliverables   # completed, "OVER BUDGET", ["output/verdict.md"]
```

**The point:** the check recomputes the answer from the raw CSV, so an agent
that reports the right total but the wrong verdict (or a skewed total) ends
`failed_checks`, and `save_verdict` never runs. Only a verified decision reaches
your records.

**Two agents, one workflow:**
- **`claude_code`** runs the script itself and self-checks before finishing.
  A real run cost about $0.06 and took 13 s.
- **`prompt_inject`** (no tools; for example a local model) can't run anything.
  The harness runs `context_commands` and puts the totals in the prompt; the
  same check still decides.

Try it: `python examples/run_budget_check.py [claude_code|prompt_inject] [budget]`
(for `prompt_inject`, set `AGENT_BASE_URL` / `AGENT_MODEL`, e.g. a local Ollama).

## How it works

### Three building blocks

| | What it is | Who writes it | Where it lives |
|---|---|---|---|
| **Skill** | A capability: an Agent Skills folder (`SKILL.md` with `name` + `description` frontmatter, plus optional `reference.md`, `scripts/`, `assets/`). It says *how* to do one kind of job and carries the scripts that do the math. Portable: the same folder works in Claude Code, the Agent SDK, or anything that reads `SKILL.md` | the app (a library), sometimes users (forks) | the caller's `skill_paths`, searched in order |
| **Workflow** | A contract: a folder with `WORKFLOW.md`. Says *what* to produce from *which* inputs, using *which* skills, under *which* policy, and *what counts as success* (outputs + check commands). Never the algorithm: the agent decides how | the app | the caller's `workflow_paths` |
| **Agent (driver)** | The rented harness that does the work: `claude_code` (tools, runs the skill's scripts itself) or `prompt_inject` (one model call, no tools) | this package | `drivers/` (more via `register_driver`) |

The app owns the skills, workflows, input staging and what happens to the
results; the agent only reasons and acts inside the workspace it is given.

### Workflow vs skill

`run_workflow` runs a `WORKFLOW.md`. `run_skill` runs one skill as the whole
task by building a small workflow in memory, so both go through the exact same
pipeline; what differs is how much contract you declare.

| | `run_workflow(name, ...)` | `run_skill(name, ...)` |
|---|---|---|
| Contract comes from | `WORKFLOW.md` frontmatter (versioned with the app) | arguments to the call |
| Inputs | declared, typed (`path` / `string`), required or optional; unknown inputs are rejected | inferred: a `Path`, or a string that is an existing absolute path, becomes a read-only path input; anything else is a plain value |
| Skills | any number, listed in `skills:` | the one skill, plus `extra_skills` it runs scripts from |
| Outputs | `outputs` (required) and `optional_outputs`, `deliverables` | `outputs` (default `output/OUTPUT.md`) and `optional_outputs` |
| Task text | the workflow body + the harness contract | "read `SKILL.md` and follow it" + optional `instructions` + the contract |
| Checks | `checks:` commands, which can live in the workflow folder (`{workflow_dir}/checks/...`) | optional `checks=` commands; otherwise only "the outputs exist" |
| Agent self-check | yes: the workflow folder is snapshotted into the workspace and the agent may run exactly its check commands before finishing | no (there is no workflow folder to snapshot) |
| Stages | optional checkpoints (`output/<stage>/`), tracked in `state.json` | none |
| Policy, secrets | declared in the workflow; the caller can override policy | caller-supplied; secrets passed are declared automatically |
| Use it when | the result feeds something that must be trusted (a write-back, a report someone acts on), the run has several skills or stages, or you want the agent to verify its own work | quick or ad-hoc runs, user-authored skills, anything whose only product is a human-readable answer |

A common pattern: start with `run_skill`; once its output is written back into
your records, give it a `WORKFLOW.md` with a check, so a bad output can never
reach them. The skill folder itself doesn't change.

### One run, step by step

```
caller ──► run_workflow / run_skill
            1. load the contract     WORKFLOW.md, or an in-memory one-skill workflow
            2. resolve               inputs (types, required, paths exist), skills (search
                                     paths; first match wins), undeclared secrets rejected
            3. policy gate           merge workflow policy + caller overrides; residency
                                     local_only refuses a cloud agent HERE, before anything
                                     is staged
            4. build the workspace   <workspace_root>/<YYYYMMDD-HHMMSS>-<workflow>-<agent>/
            5. render task.md        body + inputs + skills + stages + self-check + outputs + rules
            6. run the agent         driver streams normalized events → events.jsonl
                                     (secrets redacted); on_event per event; cancel stops it
            7. external checks       outputs exist + each check command (from the ORIGINAL
                                     workflow folder, cwd = workspace) → checks.json
            8. verdict               completed | partial | failed_checks | cancelled | error
            9. on_success            only if every check passed (completed, or partial)
           10. record                state.json + result.json (redacted), cost, duration
          ◄── RunResult (never raises for expected failures)
```

The workspace the agent sees:

```
<run>/
  AGENTS.md          the caller's agents_md (run context; also passed as system context)
  task.md            the rendered contract
  inputs/<name>      read-only symlinks to the path inputs
  .claude/skills/    copies of the resolved skills (also .agents/skills/ for other agents)
  workflow/          copy of the workflow folder (stage notes, templates, checks)
  output/            the ONLY place the agent may write
  state.json  events.jsonl  checks.json  result.json     written by the harness
```

It is its own git root, so an agent never picks up instructions or skills from
a repository the workspace happens to sit in. Skills and the workflow are
copies, so a run is reproducible even if they change later, and checks always
run from the original workflow folder, so the agent can't edit what judges it.

What differs by agent: `claude_code` gets the task as its prompt, runs the
skills' scripts and the workflow's check commands itself (nothing else is
allowlisted), and writes files with its own tools. `prompt_inject` can't use
tools, so the driver runs the workflow's `context_commands` (or inlines the
text files under `inputs/`), sends one request, and writes the files from the
model's JSON answer. Steps 1-4 and 7-10 are identical for both.

## Use

```python
from agent_harness import run_workflow, run_skill

# A workflow: WORKFLOW.md (inputs, skills, stages, outputs, checks, policy, secrets)
result = run_workflow("interpret", "claude_code", {"run_dir": path},
                      workspace_root=root / "agent_runs",
                      workflow_paths=[content / "workflows"],
                      skill_paths=[content / "skills"],
                      agents_md=(content / "AGENT_CONTEXT.md").read_text(),  # becomes the workspace AGENTS.md
                      policy={"residency": "cloud_ok"},
                      on_success=save_results)          # runs only if every check passed

# One skill as the whole task (no WORKFLOW.md)
result = run_skill("research-comps", "claude_code",
                   {"subject": staged_dir, "inputs.json": params_file},
                   workspace_root=root / "runs",
                   skill_paths=[user_skills, library_skills],   # first match wins: forks shadow the library
                   outputs=["output/comps.json"],               # required
                   optional_outputs=["output/OUTPUT.md"],       # asked for, not required
                   policy={"residency": "cloud_ok", "network": "allow"},
                   secrets={"MAPS_API_KEY": key})
result.status    # completed | partial | failed_checks | cancelled | error
result.cost_usd  # driver-reported, else estimated from tokens
```

`WORKFLOW.md` frontmatter:

| Key | Meaning |
|---|---|
| `name`, `description` | required; `name` equals the folder name |
| `inputs` | `{name: {type: path\|string, required, readonly, description}}`; path inputs appear read-only under `inputs/<name>` |
| `skills` | skill names, resolved on the caller's `skill_paths` |
| `outputs` / `optional_outputs` | files under `output/` the agent must / may write |
| `deliverables` | the subset meant for humans (default: all outputs) |
| `checks` | commands run from the workspace after the agent finishes; exit 0 = pass. Placeholders: `{python}`, `{workflow_dir}`, `{workspace}`, `{input.<name>}` |
| `policy` | `residency`, `network`, `allowed_domains`, `mcp`, `max_minutes`, `max_turns` (caller overrides win) |
| `stages` | checkpoints; progress is derived from `output/<stage>/` |
| `secrets` | env var names the caller may supply |
| `context_commands` | for tool-less agents only: commands whose output is inlined into the prompt |
| `mcp` | MCP servers the agent may call on a **live system**: `{name: {url, headers, tools, ca_cert?, description?}}`. Headers reference secrets as `{secret.NAME}` (never inline); `tools` lists exact tool names or `["*"]`. Needs `policy: {mcp: allow}` and an agent with MCP support. See [Live systems over MCP](#live-systems-over-mcp) |

## Statuses and on_success

| Status | Meaning | `on_success` runs? |
|---|---|---|
| `completed` | agent finished, every check passed | yes |
| `partial` | agent stopped early (timeout, crash) after writing outputs | only if every check passed on them |
| `failed_checks` | agent finished, a check failed | no |
| `cancelled` | the caller set its `cancel` event (`threading.Event`); the agent is stopped | no |
| `error` | nothing usable (bad input, policy refusal, agent never produced outputs, or `on_success` raised) | - |

The checks decide whether outputs are usable, not whether the agent says it
finished. `on_success` gets the `RunResult` (so it can tell `completed` from
`partial`); its return value lands in `RunResult.handled`.

CLI: `agent-harness run --workflow <name> --agent claude_code --input run_dir=<path> ...`
(`agent-harness --help`).

## Guarantees
| | |
|---|---|
| **Workspace** | its own git root (no parent-repo instructions/skills leak in); inputs are read-only symlinks under `inputs/`; skills and the workflow are snapshotted; the agent writes only to `output/` |
| **Claude Code driver** | `claude -p` headless with `--permission-mode dontAsk`, `--setting-sources project`, `--strict-mcp-config`, tool allowlists (Bash: skill scripts and self-checks, plus the read-only commands Claude Code auto-approves such as `ls`), and CLAUDE.md, memory and bundled skills disabled. Never `--dangerously-skip-permissions` |
| **Policy** | `residency: local_only` (the default) refuses cloud agents before any data is staged; `network: deny` (the default) or `allow` (+ `allowed_domains`); `mcp: deny` (the default) or `allow` for a workflow's MCP servers; time and turn limits |
| **Secrets** | declared by the workflow, passed only to the agent's process environment, redacted from `events.jsonl` and `result.json` |
| **Checks** | declared outputs must exist, and workflow check commands must pass. The agent may run exactly those commands itself (self-check); the harness re-runs them afterwards, and only its result counts. Check commands get the run's declared secrets in their environment (so they can verify a live system), and their output is redacted before it's recorded |
| **State** | `state.json`, `events.jsonl`, `checks.json`, `result.json` per run; `prune_workspaces()` for retention |

## Using it in an app

**Keep the app's agent code in one place.** Everything an app feeds the
harness lives in a single folder, and exactly one module imports
`agent_harness`:

```
<app>/
  agent_harness/
    README.md                 what the app's workflows do, how to run them
    content/
      AGENT_CONTEXT.md        passed as agents_md; becomes the workspace's AGENTS.md
      workflows/<name>/       WORKFLOW.md + checks/ (+ stages/, templates/)
      skills/<name>/          SKILL.md + scripts/ (+ reference.md)
  src/<app>/.../agent_bridge.py   the only importer: maps an app record to inputs,
                                  calls run_workflow / run_skill, handles the result
```

Pass one `workflow_paths` and one `skill_paths` list pointing into that folder,
so nothing outside it can change what an agent sees. Name the context file
something other than `AGENTS.md`: many repos ignore that name for their own
agent docs, which keeps it out of builds and deploys.

**The bridge module's job:**
- Translate the app's record into workflow inputs (usually one read-only path
  input, e.g. the record's working directory) and the app's settings into
  `agent_config` (model, endpoint, keys).
- Set `policy={"residency": ...}` from an explicit app setting. The default,
  `local_only`, refuses cloud agents before any data is staged; make sending
  data to a cloud model a deliberate opt-in.
- Put `workspace_root` next to the data the run is about, so a run's evidence
  (task, event log, checks, outputs) stays with its record.
- Copy `result.deliverables` to wherever users look for results, suffixed per
  agent (`report-<agent>.md`) if several agents can run the same workflow;
  don't send users into the workspace.
- Write anything back into the app's records only from `on_success`, which
  runs only when every check passed.
- Call `prune_workspaces(root, keep, match=...)` after each run; nothing
  prunes on its own.
- Run it off the request thread (a run takes minutes) and store the outcome
  where the UI polls: `status`, `error`, and what it cost
  (`result.cost_usd`, plus tokens and turns from `result.usage`).

**Writing workflows and skills:**
- Put the arithmetic in skill scripts and make a check recompute or re-read
  what matters. Checks decide success, and they run from the original
  workflow folder, so the agent can't edit what judges it.
- Have outputs cite their sources (file + field) so a check can verify each
  claim against the input data.
- Add `context_commands` so the same workflow also works on `prompt_inject`
  (a cheap or local model), which can't run scripts itself.
- Scripts take their input path as an argument and stay import-safe (logic in
  a function, CLI in `main`), so both the agent and your tests can use them.
- Start with `run_skill`; add a `WORKFLOW.md` with checks once the output
  feeds something people rely on.

**Testing without a model:** register a fake driver with
`register_driver("fake", FakeAgent)` whose `run()` writes the output files
and yields `AgentEvent`s (including a `usage` event if you display cost).
Point the app's bridge at it to test input mapping, residency refusal,
deliverable copying and cost recording, and run the app's real checks
against fixture data.

**Workspaces on network mounts** (sshfs, NFS): input links are relative, so
they resolve on whichever host owns the tree, and a link that some
filesystems report as failed but did create is accepted. Run the agent on
the host that owns the data where possible; it reads every file through the
mount.

## Live systems over MCP

A workflow can give the agent tools on a live system (devices, services)
through MCP, while the harness keeps owning policy and the verdict:

```markdown
---
name: system-report
description: Report the live system's state, with every claim checked.
inputs:
  endpoint: {type: string, required: true}       # e.g. https://host:8998/v1/mcp
  ca_cert: {type: path, required: false}         # PEM for a self-signed server
outputs: [output/report.md, output/facts.json]
secrets: [SYSTEM_MCP_KEY]
policy: {mcp: allow, residency: cloud_ok}
mcp:
  system:
    url: "{input.endpoint}"
    headers: {X-API-Key: "{secret.SYSTEM_MCP_KEY}"}
    tools: [list_capabilities, get_state]        # read-only: report, don't act
    ca_cert: "{input.ca_cert}"
checks:
  - "{python} {workflow_dir}/checks/verify_facts.py --facts output/facts.json"
---
```

- **Opt-in twice:** the workflow declares the servers *and* the policy says
  `mcp: allow`; a caller can override it back to `deny`. Otherwise the run
  errors before anything is staged, as it does for an agent without MCP
  support (`prompt_inject`).
- **Least privilege:** only the listed tools are allowlisted
  (`mcp__<server>__<tool>`). `["*"]` allows every tool the server offers;
  the server's own policy still applies.
- **Secrets stay out of the record:** headers reach the agent as `${NAME}`
  references its CLI expands from the process environment, so the value is
  never in the command line, `task.md` or `events.jsonl`.
- **Checks verify the world, not the report:** check commands receive the
  declared secrets, so `verify_facts.py` can query the live system and
  compare it with what the agent claimed. A wrong claim ends the run
  `failed_checks`, and `on_success` never runs.
- **Self-signed servers:** `ca_cert` becomes `NODE_EXTRA_CA_CERTS` for the
  Claude Code agent (several are bundled into the workspace).

## Roadmap

- **A second driver** (Codex or an ACP agent) exercised by more than one app.
  MCP support is a driver capability (`Capabilities.mcp`), so a new driver
  that can take MCP servers gets live-system workflows for free.

## Modules
| Module | Role |
|---|---|
| `run.py` | `run_workflow`, `run_skill` → `RunResult` |
| `workflow.py` | `WORKFLOW.md` loading, input resolution, one-skill workflows, `task.md` rendering |
| `skills.py` | `validate_skill_dir`, `list_skills` (ordered search paths), `import_skill_script` |
| `workspace.py`, `state.py` | per-run workspace, durable state, pruning |
| `runtime.py`, `drivers/` | `AgentRuntime` protocol and registry; `claude_code`, `prompt_inject` (one call, no tools: `provider: openai` for any OpenAI-compatible endpoint incl. local models, `provider: anthropic` via the official SDK; inlines `inputs/` text files when the workflow has no `context_commands`) |
| `policy.py`, `checks.py`, `pricing.py`, `events.py` | gate, verdict, cost, normalized events |

## Test

```bash
PYTHONPATH=src:. <any venv with pyyaml + pytest>/bin/python -m pytest -q
```

This package is shared by several apps. Before changing it, also run the
agent tests of every app that depends on it (any app whose `pyproject.toml`
lists `agent_harness`); they exercise the real seams.
Optional extras: `agent_harness[openai]`, `agent_harness[anthropic]`.
