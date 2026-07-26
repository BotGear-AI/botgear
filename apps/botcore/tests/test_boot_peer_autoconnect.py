# unmanaged
"""Federation peer persistence + boot autoconnect (stone 4b).

Peers persist to the active set's ``runtime.yml`` (was the runtime row's
``service_config.peers``). These cover the runtime.yml round-trip + the
boot-time dial loop's contract (dial each once, tolerate failures).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yaml  # noqa: E402

from botcore.runtime import config_sets  # noqa: E402


def _dial_peers(peer_urls: List[str], peer_manager) -> List[str]:
    """The boot autoconnect loop (mirrors event_handlers.on_startup): dial
    each persisted peer once; a failure on one must not block the others."""
    dialed: List[str] = []
    for url in peer_urls:
        try:
            peer_manager.connect(url)
            dialed.append(url)
        except Exception:
            pass
    return dialed


# ─── runtime.yml persistence ──────────────────────────────────────────


def test_peers_roundtrip_through_runtime_yml(tmp_path, monkeypatch):
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "default")
    config_sets.write_runtime_config_key(tmp_path, "peers", ["ws://a:8998/v1/ws", "ws://b:8998/v1/ws"])
    cfg = config_sets.read_runtime_config(tmp_path)
    assert cfg.get("peers") == ["ws://a:8998/v1/ws", "ws://b:8998/v1/ws"]


def test_write_runtime_key_preserves_type_and_start_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "default")
    set_dir = tmp_path / "config_sets" / "default"
    set_dir.mkdir(parents=True)
    (set_dir / "runtime.yml").write_text("type: runtime@1.0.0\nstart_order: [servo-1]\n")
    config_sets.write_runtime_config_key(tmp_path, "peers", ["ws://a"])
    raw = yaml.safe_load((set_dir / "runtime.yml").read_text())
    assert raw["type"] == "runtime@1.0.0"
    assert raw["start_order"] == ["servo-1"]
    assert raw["peers"] == ["ws://a"]
    # read_runtime_config returns settings only — never the structural keys.
    runtime_cfg = config_sets.read_runtime_config(tmp_path)
    assert runtime_cfg == {"peers": ["ws://a"]}


def test_missing_runtime_yml_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "default")
    assert config_sets.read_runtime_config(tmp_path) == {}


# ─── boot dial loop ───────────────────────────────────────────────────


def test_autoconnect_dials_each_peer_once():
    pm = MagicMock()
    dialed = _dial_peers(["ws://a:8998/v1/ws", "ws://b:8998/v1/ws"], pm)
    assert dialed == ["ws://a:8998/v1/ws", "ws://b:8998/v1/ws"]
    assert pm.connect.call_count == 2


def test_autoconnect_tolerates_one_failure():
    pm = MagicMock()
    pm.connect.side_effect = [Exception("dns failure"), MagicMock()]
    _dial_peers(["ws://bad:8998/v1/ws", "ws://good:8998/v1/ws"], pm)
    assert pm.connect.call_count == 2


def test_empty_peers_dials_nothing():
    pm = MagicMock()
    assert _dial_peers([], pm) == []
    pm.connect.assert_not_called()
