"""Canonical key-event schema + keymap matching for botgear keyboards.

Every keyboard service type — browser-captured or host-captured — publishes
the SAME event shape on ``/keyboard/{id}/event`` — ``KeyEvent`` below —
so a consumer (teleop, brain, …) subscribes by capability, not by backend.

Keymap bindings (the teleop/hotkey layer) match a ``combo`` string like
``"KeyW"`` or ``"ctrl+shift+KeyS"`` against an event. The key token matches
EITHER ``code`` or ``key`` (case-insensitive) so a binding can be written
layout-independent (``KeyW``) or logical (``w``).
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

MODIFIERS: Tuple[str, ...] = ("ctrl", "alt", "shift", "meta")


class Modifiers(BaseModel):
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    meta: bool = False


class KeyEvent(BaseModel):
    """The canonical key event, published on ``/keyboard/{id}/event``."""
    type: Literal["down", "up"]
    key: str = Field(description="Logical key (KeyboardEvent.key), e.g. 'w'")
    code: str = Field(description="Physical code (KeyboardEvent.code), e.g. 'KeyW' — layout-independent, the right thing for teleop")
    modifiers: Modifiers
    repeat: bool = Field(description="OS auto-repeat")
    ts: float = Field(description="Epoch seconds")
    source: str = Field(description="'browser' | 'local'")


def make_event(
    type_: str,
    *,
    key: str = "",
    code: str = "",
    modifiers: Optional[Dict[str, Any]] = None,
    repeat: bool = False,
    ts: float = 0.0,
    source: str = "local",
) -> Dict[str, Any]:
    """Build a normalized key-event dict. ``type_`` is coerced to
    'down'/'up'; modifiers are coerced to a full 4-key bool map."""
    mods = {m: bool((modifiers or {}).get(m)) for m in MODIFIERS}
    return KeyEvent(
        type="up" if type_ == "up" else "down",
        key=str(key or ""),
        code=str(code or ""),
        modifiers=Modifiers(**mods),
        repeat=bool(repeat),
        ts=float(ts or 0.0),
        source=source,
    ).model_dump()


def normalize_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce an arbitrary (e.g. browser-sent) dict into the canonical shape."""
    return make_event(
        ev.get("type", "down"),
        key=ev.get("key", ""),
        code=ev.get("code", ""),
        modifiers=ev.get("modifiers") if isinstance(ev.get("modifiers"), dict) else None,
        repeat=ev.get("repeat", False),
        ts=ev.get("ts", 0.0),
        source=ev.get("source", "local"),
    )


def event_token(ev: Dict[str, Any]) -> str:
    """A stable per-physical-key identity for pressed-set tracking — prefers
    the layout-independent ``code``, falls back to ``key``."""
    return str(ev.get("code") or ev.get("key") or "").lower()


def normalize_combo(combo: str) -> Tuple[Set[str], str]:
    """``"ctrl+shift+KeyS"`` → ({'ctrl','shift'}, 'keys'). The final token is
    the key (matched case-insensitively against code OR key); the rest are
    modifiers. Empty / malformed combos yield ``(set(), "")`` which never
    matches."""
    parts = [p.strip() for p in str(combo or "").split("+") if p.strip()]
    if not parts:
        return set(), ""
    *mods, key = parts
    return {m.lower() for m in mods}, key.lower()


def event_matches(ev: Dict[str, Any], mods: Set[str], key: str) -> bool:
    """True when ``ev`` has EXACTLY the modifier set ``mods`` held and its
    code/key equals ``key``. Exact-modifier match means ``ctrl+KeyS`` does
    NOT fire on ``ctrl+shift+KeyS`` — important so a hotkey isn't ambiguous."""
    if not key:
        return False
    ev_mods = {m for m in MODIFIERS if ev.get("modifiers", {}).get(m)}
    if ev_mods != mods:
        return False
    return key in (str(ev.get("code", "")).lower(), str(ev.get("key", "")).lower())


def binding_matches(ev: Dict[str, Any], binding: Dict[str, Any]) -> bool:
    """Convenience: does ``ev`` satisfy a binding's ``combo``?"""
    mods, key = normalize_combo(binding.get("combo", ""))
    return event_matches(ev, mods, key)
