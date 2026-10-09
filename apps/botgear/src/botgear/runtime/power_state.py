# unmanaged
"""Periodic /runtime/<id>/power publisher — battery, AC, suspend/resume.

Sibling of ``system_state.py`` (CPU/mem/disk/process): where that module is
the system-info dashboard, this one is the power dashboard. A control
laptop that silently suspends or runs out of battery mid-task is a real
hazard for a physical robot — this module exists to make that state
visible, not to act on it.

Topics:
  ``/runtime/<proxy_id>/power``       (retained) periodic snapshot, also
                                       republished immediately on a
                                       suspend/resume event.
  ``/runtime/<proxy_id>/power/event`` (not retained) edge-triggered
                                       ``{type: "suspend"|"resume", ts}``.

Wire shape (kept stable — see AGENTS.md "Making a new service type
discoverable" for the general convention this follows):

    {
      "battery": {                    # null if no battery present
        "present": true, "name": "BAT0",
        "percent": float, "status": "charging"|"discharging"|"full"|
                                     "not_charging"|"unknown",
        "capacity_level": str|None,
        "health_percent": float|None, # charge_full/charge_full_design*100
        "cycle_count": int|None,
        "voltage_v": float|None, "current_a": float|None,
        "energy_wh": float|None, "energy_full_wh": float|None,
        "energy_full_design_wh": float|None,
        "secs_to_empty": int|None, "secs_to_full": int|None,
        "technology": str|None, "model": str|None, "manufacturer": str|None,
      },
      "ac": {"name": str, "online": bool} | None,
      "suspend": {
        "backend": "logind-dbus" | "none",
        "supported": bool,
        "last_event": "suspend" | "resume" | None,
        "last_event_ts": float | None,
      },
      "platform_backend": "linux-sysfs" | "psutil" | "none",
    }
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

from botgear.runtime.bus import get_bus


logger = logging.getLogger(__name__)

# Overridable for tests — real sysfs root on Linux.
SYSFS_POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")

_STATUS_MAP = {
    "charging": "charging",
    "discharging": "discharging",
    "full": "full",
    "not charging": "not_charging",
}


def _parse_uevent(path: Path) -> Dict[str, str]:
    """Parse a sysfs ``uevent`` file into a ``{lowercase_key: value}`` dict
    with the ``POWER_SUPPLY_`` prefix stripped."""
    out: Dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("POWER_SUPPLY_"):
                key = key[len("POWER_SUPPLY_"):]
            out[key.lower()] = value.strip()
    except OSError:
        pass
    return out


def _as_float(fields: Dict[str, str], key: str) -> Optional[float]:
    raw = fields.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _as_int(fields: Dict[str, str], key: str) -> Optional[int]:
    raw = fields.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _battery_from_sysfs_fields(name: str, fields: Dict[str, str]) -> Dict[str, Any]:
    # Prefer charge_* (µAh); fall back to energy_* (µWh) directly. Vendors
    # expose one or the other, rarely both — same approach upower uses.
    charge_now = _as_int(fields, "charge_now")
    charge_full = _as_int(fields, "charge_full")
    charge_full_design = _as_int(fields, "charge_full_design")
    energy_now = _as_int(fields, "energy_now")
    energy_full = _as_int(fields, "energy_full")
    energy_full_design = _as_int(fields, "energy_full_design")
    voltage_v = None
    voltage_now = _as_int(fields, "voltage_now")
    if voltage_now is not None:
        voltage_v = voltage_now / 1_000_000

    def _to_wh(microamp_hours: Optional[int]) -> Optional[float]:
        if microamp_hours is None or voltage_v is None:
            return None
        return (microamp_hours / 1_000_000) * voltage_v

    if energy_now is not None or energy_full is not None or energy_full_design is not None:
        energy_wh = energy_now / 1_000_000 if energy_now is not None else None
        energy_full_wh = energy_full / 1_000_000 if energy_full is not None else None
        energy_full_design_wh = (
            energy_full_design / 1_000_000 if energy_full_design is not None else None
        )
    else:
        energy_wh = _to_wh(charge_now)
        energy_full_wh = _to_wh(charge_full)
        energy_full_design_wh = _to_wh(charge_full_design)

    health_percent = None
    if energy_full_wh is not None and energy_full_design_wh:
        health_percent = round((energy_full_wh / energy_full_design_wh) * 100, 1)

    current_a = None
    current_now = _as_int(fields, "current_now")
    if current_now is not None:
        current_a = current_now / 1_000_000

    status_raw = (fields.get("status") or "").strip().lower()
    status = _STATUS_MAP.get(status_raw, "unknown")

    secs_to_empty = None
    secs_to_full = None
    if current_now:  # non-None and non-zero
        if status == "discharging" and charge_now is not None:
            secs_to_empty = int(charge_now / current_now * 3600)
        elif status == "discharging" and energy_now is not None and voltage_v:
            secs_to_empty = int(energy_now / (current_now * voltage_v) * 3600)
        elif status == "charging" and charge_now is not None and charge_full is not None:
            secs_to_full = int((charge_full - charge_now) / current_now * 3600)
        elif status == "charging" and energy_now is not None and energy_full is not None and voltage_v:
            secs_to_full = int((energy_full - energy_now) / (current_now * voltage_v) * 3600)

    percent = _as_float(fields, "capacity")
    if percent is None and energy_full_wh and energy_wh is not None:
        percent = round((energy_wh / energy_full_wh) * 100, 1)

    return {
        "present": fields.get("present", "1") == "1",
        "name": name,
        "percent": percent,
        "status": status,
        "capacity_level": fields.get("capacity_level"),
        "health_percent": health_percent,
        "cycle_count": _as_int(fields, "cycle_count"),
        "voltage_v": voltage_v,
        "current_a": current_a,
        "energy_wh": energy_wh,
        "energy_full_wh": energy_full_wh,
        "energy_full_design_wh": energy_full_design_wh,
        "secs_to_empty": secs_to_empty,
        "secs_to_full": secs_to_full,
        "technology": fields.get("technology"),
        "model": fields.get("model_name"),
        "manufacturer": fields.get("manufacturer"),
    }


def _collect_linux_sysfs(root: Path) -> Optional[Dict[str, Any]]:
    """Returns ``{"battery": ..., "ac": ...}`` or None if the sysfs root
    has no readable power-supply entries at all (caller falls back)."""
    if not root.is_dir():
        return None
    battery = None
    ac = None
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        fields = _parse_uevent(entry / "uevent")
        if not fields:
            continue
        supply_type = (fields.get("type") or "").strip().lower()
        if supply_type == "battery" and battery is None:
            battery = _battery_from_sysfs_fields(entry.name, fields)
        elif supply_type in ("mains", "usb") and ac is None:
            ac = {"name": entry.name, "online": fields.get("online") == "1"}
    if battery is None and ac is None:
        return None
    return {"battery": battery, "ac": ac}


def _collect_psutil_fallback() -> Dict[str, Any]:
    """Cross-platform fallback (macOS/Windows, or a Linux host without
    readable /sys/class/power_supply). Coarser than the sysfs path — no
    health/cycle-count/voltage/current, one aggregate battery."""
    try:
        sb = psutil.sensors_battery()
    except Exception:  # noqa: BLE001 — platform quirks, never fatal
        sb = None
    if sb is None:
        return {"battery": None, "ac": None}
    secs = sb.secsleft if isinstance(sb.secsleft, int) else None
    battery = {
        "present": True,
        "name": None,
        "percent": round(float(sb.percent), 1),
        "status": "charging" if sb.power_plugged and sb.percent < 100 else (
            "full" if sb.power_plugged else "discharging"
        ),
        "capacity_level": None,
        "health_percent": None,
        "cycle_count": None,
        "voltage_v": None,
        "current_a": None,
        "energy_wh": None,
        "energy_full_wh": None,
        "energy_full_design_wh": None,
        "secs_to_empty": secs if not sb.power_plugged else None,
        "secs_to_full": None,
        "technology": None,
        "model": None,
        "manufacturer": None,
    }
    ac = {"name": None, "online": bool(sb.power_plugged)}
    return {"battery": battery, "ac": ac}


class PowerStatePublisher:
    """Background task publishing ``/runtime/<id>/power`` on an interval,
    plus a logind-backed suspend/resume watcher (Linux only).

    Use as::

        pub = PowerStatePublisher(proxy_id="runtime", interval_s=5.0)
        pub.start()
        ...
        await pub.stop()
    """

    def __init__(self, proxy_id: str = "runtime", interval_s: float = 5.0) -> None:
        self.proxy_id = proxy_id
        self.interval_s = interval_s
        self.topic = f"/runtime/{proxy_id}/power"
        self.event_topic = f"/runtime/{proxy_id}/power/event"
        self._task: Optional[asyncio.Task[None]] = None
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._suspend_backend = "none"
        self._suspend_supported = False
        self._last_event: Optional[str] = None
        self._last_event_ts: Optional[float] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="runtime.power_state")
        self._watch_task = asyncio.create_task(
            self._watch_logind(), name="runtime.power_state.logind"
        )
        logger.info("runtime.power_state: started (topic=%s interval=%.1fs)",
                    self.topic, self.interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await asyncio.wait_for(self._watch_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._task = None
        self._watch_task = None
        logger.info("runtime.power_state: stopped")

    async def _run(self) -> None:
        while True:
            try:
                get_bus().publish_sync(self.topic, self._snapshot(), retained=True)
            except Exception:  # noqa: BLE001
                logger.exception("runtime.power_state: snapshot/publish failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except asyncio.TimeoutError:
                continue

    async def _watch_logind(self) -> None:
        """Subscribe to logind's PrepareForSleep signal on the system bus.
        Any failure (no dbus, no logind, sandboxed/headless environment)
        is caught and logged once — the periodic battery/AC snapshot
        keeps working regardless; this just means suspend.supported stays
        False."""
        try:
            from jeepney.bus_messages import MatchRule, message_bus
            from jeepney.io.asyncio import Proxy, open_dbus_router
        except ImportError:
            logger.info("runtime.power_state: jeepney not installed — suspend/resume detection disabled")
            return

        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.login1.Manager",
            member="PrepareForSleep",
            path="/org/freedesktop/login1",
        )
        try:
            async with open_dbus_router(bus="SYSTEM") as router:
                await Proxy(message_bus, router).AddMatch(rule)
                self._suspend_backend = "logind-dbus"
                self._suspend_supported = True
                logger.info("runtime.power_state: watching logind PrepareForSleep")
                with router.filter(rule) as queue:
                    while True:
                        msg = await queue.get()
                        (starting,) = msg.body
                        self._last_event = "suspend" if starting else "resume"
                        self._last_event_ts = time.time()
                        logger.info("runtime.power_state: %s detected", self._last_event)
                        try:
                            get_bus().publish_sync(self.topic, self._snapshot(), retained=True)
                            get_bus().publish_sync(
                                self.event_topic,
                                {"type": self._last_event, "ts": self._last_event_ts},
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception("runtime.power_state: suspend-event publish failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — no system bus / no logind / permission issue
            self._suspend_backend = "none"
            self._suspend_supported = False
            logger.info(
                "runtime.power_state: logind PrepareForSleep unavailable (%s) — "
                "suspend/resume detection disabled", exc,
            )

    def _snapshot(self) -> Dict[str, Any]:
        collected = _collect_linux_sysfs(SYSFS_POWER_SUPPLY_ROOT)
        platform_backend = "linux-sysfs"
        if collected is None:
            collected = _collect_psutil_fallback()
            platform_backend = "none" if collected["battery"] is None and collected["ac"] is None else "psutil"

        return {
            "battery": collected["battery"],
            "ac": collected["ac"],
            "suspend": {
                "backend": self._suspend_backend,
                "supported": self._suspend_supported,
                "last_event": self._last_event,
                "last_event_ts": self._last_event_ts,
            },
            "platform_backend": platform_backend,
        }


# Module-level singleton — mirrors system_state.py's convention.
_publisher: Optional[PowerStatePublisher] = None


def start_publisher(proxy_id: str = "runtime", interval_s: float = 5.0) -> None:
    """Idempotent — calling twice is a no-op."""
    global _publisher
    if _publisher is None:
        _publisher = PowerStatePublisher(proxy_id=proxy_id, interval_s=interval_s)
    _publisher.start()


async def stop_publisher() -> None:
    """Best-effort shutdown for tests + clean exits."""
    global _publisher
    if _publisher is not None:
        await _publisher.stop()
        _publisher = None
