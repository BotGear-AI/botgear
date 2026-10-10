from __future__ import annotations

import pytest

from agent_harness.policy import Policy, PolicyError


def test_defaults_are_restrictive():
    p = Policy.from_dict()
    assert p.residency == "local_only" and p.network == "deny"


def test_layers_merge_left_to_right():
    p = Policy.from_dict({"max_minutes": 5, "residency": "local_only"}, {"residency": "cloud_ok"})
    assert p.max_minutes == 5 and p.residency == "cloud_ok"


def test_unknown_and_invalid_keys_rejected():
    with pytest.raises(PolicyError):
        Policy.from_dict({"residensy": "cloud_ok"})
    with pytest.raises(PolicyError):
        Policy.from_dict({"residency": "anywhere"})
