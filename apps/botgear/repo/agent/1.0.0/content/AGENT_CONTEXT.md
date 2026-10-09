# botgear: context for agent runs

You are working on a live **botgear** robot runtime: a set of services (speech,
speech-to-text, microphone, speaker, smart switches, network scanner, …)
connected by a message bus. You reach it only through the `botgear` MCP
server's tools, which act on the real robot, right now.

- Tools are named `<service>__<action>` (e.g. `switch-1__list_relays`) plus
  three built-ins: `botgear_list_capabilities` (what's running, start here),
  `botgear_get_state` (a service's current state, read-only) and
  `botgear_halt` (emergency stop).
- A service id (`switch-1`, `mouth`, `ear`) is an instance; its capability
  (`tasmota`, `speech`, `stt`) is its type.
- If the runtime reports `halted: true`, an operator stopped it: report that,
  don't try to work around it.
- Never change anything the task doesn't ask you to change. Prefer reading
  state over calling actions.
- Base every statement on a tool result from this run, and say which tool
  and field it came from.
