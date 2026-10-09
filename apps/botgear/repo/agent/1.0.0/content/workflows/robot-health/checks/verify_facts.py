"""robot-health check: re-read the live robot and compare it with what the
agent claimed in output/facts.json. Exit 0 only if every claim matches.

Uses the same MCP gateway the agent used (the harness hands checks the
run's declared secrets, so BOTGEAR_MCP_KEY is in the environment), but makes
its own calls: the agent's report is never trusted on its own.

    python verify_facts.py --facts output/facts.json --endpoint https://localhost:8998/v1/mcp --ca ssl/cert.pem
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Dict, List

MIN_CLAIMS = 5


def lookup(state: Any, path: str) -> Any:
    """Follow a dot path (``relays.0.on``) into a JSON value; KeyError if absent."""
    node = state
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise KeyError(path)
    return node


def compare(facts: Dict[str, Any], capabilities: Dict[str, Any], states: Dict[str, Any]) -> List[str]:
    """FAIL lines for every mismatch between the agent's facts and the live
    reads (``states``: service -> state, or an Exception for unreadable)."""
    fails: List[str] = []
    live_services = sorted({i for cap in capabilities.get("capabilities", {}).values() for i in cap.get("instances", [])})
    claimed = sorted(facts.get("services") or [])
    if claimed != live_services:
        fails.append(f"FAIL: services {claimed} != live {live_services} "
                     f"(missing {sorted(set(live_services) - set(claimed))}, extra {sorted(set(claimed) - set(live_services))})")
    if bool(facts.get("halted")) != bool(capabilities.get("halted")):
        fails.append(f"FAIL: halted {facts.get('halted')!r} != live {capabilities.get('halted')!r}")
    claims = facts.get("claims") or []
    if len(claims) < MIN_CLAIMS:
        fails.append(f"FAIL: only {len(claims)} claims, need at least {MIN_CLAIMS}")
    for c in claims:
        service, path, value = c.get("service"), str(c.get("path") or ""), c.get("value")
        state = states.get(service)
        if isinstance(state, Exception) or state is None:
            fails.append(f"FAIL: claim {service}.{path}: state not readable ({state})")
            continue
        try:
            live = lookup(state, path)
        except (KeyError, IndexError, ValueError):
            fails.append(f"FAIL: claim {service}.{path}: no such field in live state")
            continue
        if live != value:
            fails.append(f"FAIL: claim {service}.{path} = {value!r}, live {live!r}")
    return fails


async def read_live(endpoint: str, ca: str, key: str, services: List[str]):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    def payload(result) -> Any:
        if result.is_error:
            raise RuntimeError(result.content[0].text if result.content else "error")
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    http = httpx2.AsyncClient(verify=ssl.create_default_context(cafile=ca), headers={"X-API-Key": key}, timeout=30)
    async with http, streamable_http_client(endpoint, http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            capabilities = payload(await session.call_tool("botgear_list_capabilities", {}))
            states: Dict[str, Any] = {}
            for service in services:
                try:
                    states[service] = payload(await session.call_tool("botgear_get_state", {"service": service}))
                except Exception as exc:  # noqa: BLE001 — reported per claim
                    states[service] = exc
    return capabilities, states


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--ca", required=True)
    args = ap.parse_args(argv)
    key = os.environ.get("BOTGEAR_MCP_KEY")
    if not key:
        print("FAIL: BOTGEAR_MCP_KEY is not set (the harness passes declared secrets to checks)")
        return 1
    try:
        facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: {args.facts} unreadable: {exc}")
        return 1
    services = sorted({c.get("service") for c in facts.get("claims") or [] if c.get("service")})
    try:
        capabilities, states = asyncio.run(read_live(args.endpoint, args.ca, key, services))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not read the live robot: {type(exc).__name__}: {exc}")
        return 1
    fails = compare(facts, capabilities, states)
    for line in fails:
        print(line)
    if not fails:
        print(f"OK: {len(facts.get('claims') or [])} claims and {len(facts.get('services') or [])} services match the live robot")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
