# unmanaged
"""botgear's robot-health agent workflow (repo/agent/1.0.0/content): the
contract it declares, and the check that compares the agent's claims with
the live robot (pure parts; the live read is exercised by a real run)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_harness.workflow import load_workflow

CONTENT = Path(__file__).resolve().parents[1] / "repo" / "agent" / "1.0.0" / "content"
CHECK = CONTENT / "workflows" / "robot-health" / "checks" / "verify_facts.py"


def _check():
    spec = importlib.util.spec_from_file_location("verify_facts", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_workflow_contract_is_read_only_and_needs_opt_ins():
    wf = load_workflow("robot-health", [CONTENT / "workflows"])
    server = wf.mcp["botgear"]
    assert server.tools == ["botgear_list_capabilities", "botgear_get_state"]   # no actions
    assert server.secret_refs() == {"BOTGEAR_MCP_KEY"} and wf.secrets == ["BOTGEAR_MCP_KEY"]
    assert wf.policy.get("mcp") == "allow"
    assert "residency" not in wf.policy                                            # the caller decides cloud
    assert wf.outputs == ["output/facts.json", "output/report.md"]
    assert (CONTENT / "AGENT_CONTEXT.md").is_file()


CAPS = {"halted": False, "capabilities": {
    "tasmota": {"instances": ["switch-1"]}, "speech": {"instances": ["mouth"]}, "stt": {"instances": ["ear"]}}}
STATES = {
    "switch-1": {"relays": [{"index": 1, "on": True}], "connected": True},
    "mouth": {"voice": "en_GB-alan-medium", "muted": False, "last_error": None},
    "ear": {"continuous": True, "muted": False},
}


def _facts(**kw):
    facts = {"halted": False, "services": ["ear", "mouth", "switch-1"], "claims": [
        {"service": "switch-1", "path": "relays.0.on", "value": True},
        {"service": "switch-1", "path": "connected", "value": True},
        {"service": "mouth", "path": "voice", "value": "en_GB-alan-medium"},
        {"service": "mouth", "path": "last_error", "value": None},
        {"service": "ear", "path": "continuous", "value": True},
    ]}
    facts.update(kw)
    return facts


def test_matching_facts_pass():
    assert _check().compare(_facts(), CAPS, STATES) == []


@pytest.mark.parametrize("facts, needle", [
    (_facts(services=["mouth", "switch-1"]), "missing ['ear']"),
    (_facts(halted=True), "halted"),
    (_facts(claims=_facts()["claims"][:3]), "need at least 5"),
    (_facts(claims=_facts()["claims"][:4] + [{"service": "ear", "path": "continuous", "value": False}]),
     "ear.continuous = False, live True"),
    (_facts(claims=_facts()["claims"][:4] + [{"service": "ear", "path": "nope.x", "value": 1}]), "no such field"),
    (_facts(claims=_facts()["claims"][:4] + [{"service": "switch-1", "path": "relays.3.on", "value": True}]),
     "no such field"),
])
def test_wrong_facts_fail(facts, needle):
    fails = _check().compare(facts, CAPS, STATES)
    assert any(needle in f for f in fails), fails


def test_unreadable_state_fails_the_claim():
    states = {**STATES, "ear": RuntimeError("no state")}
    assert any("not readable" in f for f in _check().compare(_facts(), CAPS, states))


def test_check_without_key_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BOTGEAR_MCP_KEY", raising=False)
    facts = tmp_path / "facts.json"
    facts.write_text("{}")
    assert _check().main(["--facts", str(facts), "--endpoint", "https://x/v1/mcp", "--ca", "c.pem"]) == 1
    assert "BOTGEAR_MCP_KEY" in capsys.readouterr().out
