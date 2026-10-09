"""rlx_bus — WebSocket bus client + subprocess-service helpers for botgear.

Public surface:

    BusClient            Construct manually with backend_url + token.
    from_env             Build a BusClient from process_manager-injected env vars.
    SubprocessService    Base class for subprocess services. Mirrors
                         botgear.framework.Service so writing a service
                         feels the same regardless of transport.
    service_method       Decorator marking a callable as bus-routable + discoverable.
    MethodInfo           Per-method metadata returned by ``methods()``.
    describe_class       Cross-venv JSON-Schema introspection (config/
                         state/topic schemas + methods) — see
                         ``introspect.py``. The backend calls this
                         in-process for in-process types, and via a
                         one-shot subprocess (through the type's own
                         venv) for subprocess types — one implementation,
                         both paths.
"""
from .bus_serial import BusBackedSerial
from .client import BusClient, from_env
from .config import ServiceConfig
from .halt import HALT_LEVELS, HALT_STATE_TOPIC, HALT_TOPIC, RESUME_TOPIC, halt_event
from .introspect import describe_class
from .methods import MethodInfo, collect_methods, service_method
from .serial_ports import list_ports as list_serial_ports, scan_port_holders
from .service import SubprocessService
from .streams import Stream

__all__ = [
    "BusClient",
    "BusBackedSerial",
    "describe_class",
    "from_env",
    "MethodInfo",
    "collect_methods",
    "service_method",
    "ServiceConfig",
    "Stream",
    "SubprocessService",
    # Runtime-wide halt (see halt.py).
    "HALT_LEVELS",
    "HALT_STATE_TOPIC",
    "HALT_TOPIC",
    "RESUME_TOPIC",
    "halt_event",
    # Serial port enumeration — shared so every service that opens
    # /dev/tty* gets the same dropdown + ownership story without
    # duplicating the logic per service.
    "list_serial_ports",
    "scan_port_holders",
]
__version__ = "0.3.0"
