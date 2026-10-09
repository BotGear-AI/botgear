# unmanaged
"""Type-descriptor schema introspection — thin botgear-side wrappers.

The actual JSON-Schema logic (config/state/topic schemas + method arg
schemas) lives in ``rlx_bus.introspect`` — shared with every subprocess
type's own venv, since ``rlx_bus`` is the one package guaranteed
installed there (see ``installer.py:_rlx_bus_local_path``). This file
used to duplicate that logic locally; it now just imports it, so the
in-process and subprocess introspection paths can never drift apart.

This module additionally owns the one thing that's genuinely specific
to the SUBPROCESS path: running ``rlx_bus.introspect.describe_class``
inside a type's own venv via a one-shot subprocess, because the
backend process can't import that venv's Service class directly.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rlx_bus.introspect import (
    args_schema_for as method_args_schema,
    config_schema as config_class_schema,
    describe_class,
    state_schema as class_state_schema,
    topic_schemas as class_topic_schemas,
    subscribes_schemas as class_subscribes_schemas,
    methods as introspect_methods,
)

__all__ = [
    "config_class_schema",
    "method_args_schema",
    "class_state_schema",
    "class_topic_schemas",
    "class_subscribes_schemas",
    "introspect_methods",
    "subprocess_module_class",
    "subprocess_introspect",
]

logger = logging.getLogger(__name__)


# ─── subprocess introspection (venv one-shot script) ────────────────
#
# Subprocess service types (oakd, video, arduino, discord, ...) declare
# their Service class in a per-type venv the backend process can't
# import directly (that's the whole point of isolating them — their
# third-party SDKs are large/version-coupled-to-hardware). But nothing
# stops us from asking THAT venv's own interpreter to import the class
# and hand back rlx_bus.introspect.describe_class(cls) as plain JSON on
# stdout — no live subprocess service needs to be running, this is a
# one-shot script run via the venv's python, same pattern
# ``installer.py`` already uses to pip-install into a type's venv.

# Every existing subprocess service follows one convention (see
# ``entry.argv: [python, -m, <package>]`` in package.yml):
#   module   = "<package>.service"
#   class    = "".join(capitalize(part) for part in <type_name>.split("_")) + "Service"
# e.g. type "arduino_telemetrix" -> package "arduino_telemetrix_service"
# -> module "arduino_telemetrix_service.service", class
# "ArduinoTelemetrixService". Same class-name formula
# ``boot.resolve_config_class``/``discovery._row_to_config_instance``
# already use for in-process entries.
_SUBPROCESS_INTROSPECT_SCRIPT = (
    "import importlib, json, sys\n"
    "from rlx_bus.introspect import describe_class\n"
    "module_name, class_name = sys.argv[1], sys.argv[2]\n"
    "try:\n"
    "    mod = importlib.import_module(module_name)\n"
    "    cls = getattr(mod, class_name, None)\n"
    "    if cls is None:\n"
    "        # Case-mismatch fallback for names like 'WebXRService' vs the\n"
    "        # guessed 'WebxrService' -- match case-insensitively.\n"
    "        for attr_name in dir(mod):\n"
    "            if attr_name.lower() == class_name.lower():\n"
    "                cls = getattr(mod, attr_name)\n"
    "                break\n"
    "    if cls is None:\n"
    "        raise AttributeError(f'no {class_name!r} (case-insensitive) in {module_name}')\n"
    "    print(json.dumps(describe_class(cls)))\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'__error__': repr(exc)}))\n"
)

_SUBPROCESS_INTROSPECT_TIMEOUT_SECONDS = 15.0


def subprocess_module_class(type_name: str, argv: List[str]) -> Optional[Tuple[str, str]]:
    """Best-effort ``(module, class_name)`` for a subprocess type's
    Service subclass, derived from its ``entry.argv``. Returns None when
    argv doesn't match the ``[..., "-m", "<package>", ...]`` shape every
    stock subprocess service uses.
    """
    if not argv or "-m" not in argv:
        return None
    idx = argv.index("-m")
    if idx + 1 >= len(argv):
        return None
    package = argv[idx + 1]
    module = f"{package}.service"
    class_name = "".join(p.capitalize() for p in type_name.split("_")) + "Service"
    return module, class_name


def subprocess_introspect(
    venv_python: Path,
    type_name: str,
    argv: List[str],
    *,
    timeout: float = _SUBPROCESS_INTROSPECT_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Introspect a subprocess type's config/state/topic schemas +
    methods by running a one-shot script inside the type's OWN venv
    (the only place its Service class — and the third-party deps it
    may import at module scope — can be imported).

    Returns ``rlx_bus.introspect.describe_class``'s output —
    ``{"config_schema": ..., "state_schema": ..., "topic_schemas": {...},
    "methods": [...]}`` — or None on any failure: no venv yet (type
    never installed), argv doesn't match the ``-m <package>``
    convention, or the module/class can't be found. An empty/null-valued
    result dict is a valid SUCCESS (the class just declares none of
    these) — distinct from None (introspection itself couldn't run).
    Never raises — this feeds a best-effort descriptor, not a hard
    dependency.
    """
    if not venv_python.is_file():
        return None
    resolved = subprocess_module_class(type_name, argv)
    if resolved is None:
        return None
    module_name, class_name = resolved
    try:
        proc = subprocess.run(
            [str(venv_python), "-c", _SUBPROCESS_INTROSPECT_SCRIPT, module_name, class_name],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("subprocess_introspect: %s introspection failed to run: %s", type_name, exc)
        return None
    if proc.returncode != 0:
        logger.info("subprocess_introspect: %s exited %d: %s",
                     type_name, proc.returncode, (proc.stderr or "").strip()[-500:])
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        result = json.loads(raw)
    except ValueError:
        logger.info("subprocess_introspect: %s produced non-JSON output", type_name)
        return None
    if not isinstance(result, dict):
        return None
    if "__error__" in result:
        logger.info("subprocess_introspect: %s introspection error: %s",
                     type_name, result["__error__"])
        return None
    return result
