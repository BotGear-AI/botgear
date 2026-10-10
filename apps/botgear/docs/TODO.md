# TODO — botgear backlog (misc)

Groomed 2026-10-10. Done items were removed: `@service_method` +
`SubprocessService` live in `rlx_bus`; the repo and catalog pages are one
Catalog page; the stream channel + `video` service shipped (see
`repo/video/1.0.0/README.md`); LLM tool descriptions are generated from
`@service_method` signatures by `agent_surface`. The ARM runner item moved
to `../TODO_CROSS_PLATFORM_RELEASE.md`.

## Don't adopt stale subprocesses after a backend crash

When the backend restarts, `runtime/discovery.py` adopts any surviving
subprocess that says `hello` and keeps its pid. If the previous backend
*died* (rather than a graceful `POST /v1/system/restart`, which drains
services first), the survivor's internal state — pymata4 threads, bus
client, serial handles — can be stale, and its first action fails (e.g.
arduino `connect` → `BrokenPipeError`); today the fix is Stop + Start.

Fix: in the startup reconcile, if a surviving subprocess's parent is no
longer the current backend (reparented to init / systemd-user —
`proc_compat.proc_ppid`), stop it and spawn fresh instead of adopting.

## Document in-process restart semantics

In-process services (`language: builtin`) die with the backend and are
re-spawned by `reconcile_running_proxies`; they are restartable, not
persistent. That's harmless while they're stateless (clock, echo). Either
state the rule in AGENTS.md ("in-process services keep no state across a
restart — persist it in config") or give in-process services a
`hello`-equivalent so discovery can confirm them like subprocesses.

## Ideas (unscoped)

- Kinematics service that learns from data (LLM / ML) — input and compute
  the matrices of output.
- Five cameras around the Fab Lab to judge success and collect training
  data for the LLM / ML work.
- Firmata: better on-board speed control and dynamic device switching for
  low-memory boards.
