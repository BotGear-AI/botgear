# unmanaged
"""types_index.rebuild — config_schema reaches the service_meta row the
UI reads, for both transports."""
from __future__ import annotations

from botgear.runtime import types_index
from botgear.runtime.bus import Bus


def test_rebuild_persists_config_schema_for_both_transports(monkeypatch):
    schema = {"type": "object", "properties": {"residency": {"type": "string"}}}
    rows = [
        {"id": "agent@1.0.0", "name": "agent"},          # in-process (builtin)
        {"id": "tasmota@1.0.0", "name": "tasmota"},      # subprocess
        {"id": "echo@1.0.0", "name": "echo"},            # no config_class
    ]
    transports = {"agent": "in_process", "tasmota": "subprocess", "echo": "in_process"}
    monkeypatch.setattr(types_index, "_resolve_repo_dir", lambda: None)
    monkeypatch.setattr(types_index, "_read_service_meta_rows", lambda: rows)
    monkeypatch.setattr(types_index, "_build_descriptor", lambda meta, repo: {
        "type": meta["name"], "version": "1.0.0", "transport": transports[meta["name"]],
        "config_schema": None if meta["name"] == "echo" else schema,
    })
    monkeypatch.setattr(types_index, "get_bus", lambda: Bus())
    monkeypatch.setattr(types_index, "_built", {})
    persisted = []
    monkeypatch.setattr(types_index, "_persist_config_schema",
                        lambda meta, s: persisted.append(meta["id"]))
    types_index.rebuild()
    # Built-in agent included: without it the UI had no Configure button.
    assert persisted == ["agent@1.0.0", "tasmota@1.0.0"]
