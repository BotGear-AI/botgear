# unmanaged
"""Unit tests for WebXRService's gamepad-compatibility reshaping.

_joystick_payload is the one piece of webxr_service worth unit-testing
in isolation: it's a pure function (no bus, no asyncio), and it's the
entire contract motor_control's input_source binding relies on to treat
a Quest controller exactly like a physical joystick. Everything else in
this service needs a live SubprocessService + bus to exercise.
"""
from __future__ import annotations

import sys
from pathlib import Path

_WEBXR_SRC = Path(__file__).resolve().parents[1] / "repo" / "webxr" / "1.0.0" / "src"
if str(_WEBXR_SRC) not in sys.path:
    sys.path.insert(0, str(_WEBXR_SRC))

from webxr_service.service import _joystick_payload  # noqa: E402


def test_joystick_payload_maps_axes_xy_in_order():
    entry = {"axes": {"x": 0.02, "y": -0.99}, "buttons": {}}
    out = _joystick_payload(entry, now=123.0)
    assert out["axes"] == [0.02, -0.99]
    assert out["ts"] == 123.0


def test_joystick_payload_second_element_is_y_matching_physical_joystick_convention():
    """The whole point: index 1 (the 2nd array element) must be the
    thumbstick Y, same as repo/joystick publishes for a real controller's
    stick — that's what lets a motor_control channel tuned against a
    physical joystick (field=axes, index=1) be re-pointed here with only
    a topic-string change."""
    forward = _joystick_payload({"axes": {"x": 0.0, "y": -0.99}, "buttons": {}}, now=0.0)
    backward = _joystick_payload({"axes": {"x": 0.0, "y": 0.99}, "buttons": {}}, now=0.0)
    assert forward["axes"][1] == -0.99
    assert backward["axes"][1] == 0.99


def test_joystick_payload_defaults_missing_axes_to_zero():
    out = _joystick_payload({}, now=0.0)
    assert out["axes"] == [0.0, 0.0]


def test_joystick_payload_buttons_array_order_and_fallback():
    # a/x share slot 3, b/y share slot 4 — left controller reports x/y,
    # right controller reports a/b, but both land in the same index so a
    # single motor_control button binding works for either hand.
    left = _joystick_payload({"axes": {}, "buttons": {"trigger": True, "x": True}}, now=0.0)
    right = _joystick_payload({"axes": {}, "buttons": {"grip": True, "b": True}}, now=0.0)
    assert left["buttons"] == [1, 0, 0, 1, 0]
    assert right["buttons"] == [0, 1, 0, 0, 1]


def test_joystick_payload_handles_missing_buttons_dict():
    out = _joystick_payload({"axes": {"x": 1.0, "y": 1.0}}, now=0.0)
    assert out["buttons"] == [0, 0, 0, 0, 0]
