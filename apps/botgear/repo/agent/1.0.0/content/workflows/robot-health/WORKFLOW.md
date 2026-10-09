---
name: robot-health
description: Report what is running on the robot and whether anything looks wrong, with every fact checked against the live robot.
inputs:
  endpoint: {type: string, required: true, description: "the botgear MCP endpoint, e.g. https://localhost:8998/v1/mcp"}
  ca_cert: {type: path, required: true, description: "PEM of the backend's (self-signed) TLS cert"}
outputs: [output/facts.json, output/report.md]
deliverables: [output/report.md]
secrets: [BOTGEAR_MCP_KEY]
policy: {mcp: allow, max_minutes: 10}
mcp:
  botgear:
    url: "{input.endpoint}"
    headers: {X-API-Key: "{secret.BOTGEAR_MCP_KEY}"}
    ca_cert: "{input.ca_cert}"
    description: the live botgear robot runtime (read-only for this task)
    tools: [botgear_list_capabilities, botgear_get_state]
checks:
  - "{python} {workflow_dir}/checks/verify_facts.py --facts output/facts.json --endpoint {input.endpoint} --ca {input.ca_cert}"
---
Produce a health report of the live robot. This task is **read-only**: you
have only `botgear_list_capabilities` and `botgear_get_state`; don't try to
change anything.

1. Call `botgear_list_capabilities`: note whether the runtime is halted and
   every capability with its service instances.
2. Call `botgear_get_state` for each service instance (skip `brain`, `runtime`
   and `security`; a service that has published no state yet is a finding,
   not a failure).
3. Write `output/facts.json`, exactly this shape:

   ```json
   {
     "halted": false,
     "services": ["ear", "mouth", "switch-1"],
     "claims": [
       {"service": "switch-1", "path": "relays.0.on", "value": true},
       {"service": "mouth", "path": "voice", "value": "en_GB-alan-medium"}
     ]
   }
   ```

   - `services`: every service instance `botgear_list_capabilities` listed,
     sorted, none left out.
   - `claims`: at least 5 facts you will rely on in the report, each copied
     **exactly** from a `botgear_get_state` result: `path` is dot-separated
     keys, with list indexes as numbers (`relays.0.on`). Prefer settings and
     status fields (connected, muted, voice, model, relays, errors) over
     fast-changing ones (levels, timestamps, counters).
4. Write `output/report.md` for an operator: a one-line verdict ("All N
   services look healthy" / "K issues"), then one line per service (what it
   is, its key state), then any issues: errors (`last_error` set), things
   disconnected or muted that you'd expect active, missing state. Back each
   issue with its service and field.

The check re-reads the robot independently: the service list and every claim
must match what it sees.
