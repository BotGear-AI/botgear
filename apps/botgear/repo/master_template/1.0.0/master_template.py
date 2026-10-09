# unmanaged
"""master_template — reference service implementation.

This is the in-process variant (matches package.yml's
``install.kind: builtin`` + ``entry.in_process``). For the subprocess
variant, see ``repo/echo_http/1.0.0/src/echo_http/__main__.py`` —
the contract is the same (subscribe + publish over the bus) but the
service runs in a child process and uses ``rlx_bus`` directly instead
of inheriting from ``framework.Service``.

Wire shape is declared in code below (``MasterTemplateState``,
``ExampleEventPayload``, ``state_schema``/``topic_schemas``) —
introspected into this type's descriptor at
``/runtime/runtime/types/master_template`` (see
``runtime/types_index.py``), not hand-documented in this docstring.
``control`` actions (``do_thing``/``set_message``) are discoverable the
same way, off their @service_method signatures.

This service doesn't subscribe to anything of its own, so there's no
``subscribes_schemas`` example here — see ``repo/chat/1.0.0/chat.py``'s
``inbox`` for the worked inbound-topic example (it needed a real
subscribe loop + queue to demonstrate honestly; duplicating that
machinery in a reference file already covering the rest of the
framework surface would be its own source of drift).

What ``botgear.framework.Service`` actually gives you, for free, just
from subclassing it (see ``framework/service.py`` for the real surface
— this list intentionally names only what's real, since a previous
version of this file listed methods that don't exist and every author
who copied it hit an ``AttributeError`` on first run):
  * ``self.config`` — a typed instance of ``config_class``, hydrated
    from ``service_proxy.service_config`` before ``on_start`` runs
  * ``save_config()``/``get_config()``/``set_config(patch)``/
    ``reload_config()`` — auto-mounted @service_method actions for
    config persistence; you don't write these yourself
  * ``self.publish(suffix, payload, retained=False)`` — publish to
    ``/<type>/<proxy_id>/<suffix>``
  * ``self.subscribe_iter(suffix)`` — async-iterate messages on a topic
  * a ``/control`` dispatcher (wire it up yourself via
    ``asyncio.create_task(self.run_control_loop())`` in ``on_start`` —
    see below) that routes ``{"action": name, ...}`` to your
    @service_method handlers, with reply_to + auto-publish hooks
  * graceful ``on_start``/``on_stop`` lifecycle hooks

There is NO generic ``publish_state()``/``update_config()`` helper, no
built-in heartbeat, no ``_snapshot()`` hook, no ``type_name`` class
attribute (the type name comes from ``self.meta.type_name`` — the
framework sets it from package.yml, you never declare it on the
class). Retained-state publishing and config mutation are conventions
every service writes for itself — ``_publish_state()`` below and
``set_message()``'s ``merge_dict`` call are the pattern to copy, the
same one ``clock``/``chat`` use.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from rlx_bus import ServiceConfig
from botgear.framework import Service, service_method


logger = logging.getLogger(__name__)


class MasterTemplateConfig(ServiceConfig):
    """Strongly-typed config — fields here correspond to the
    wizard_config entries in package.yml. The runtime hydrates this
    from service_proxy.service_config on every start, so the wizard's
    values flow through verbatim.
    """
    example_message: str = Field(
        "hello from master_template",
        description="Free-text example",
    )
    tick_count: int = Field(5, description="How many demo ticks to count")
    enabled: bool = Field(True, description="Toggle the demo loop")


# ── wire schemas: these ARE the docs, not a docstring block ──────────

class MasterTemplateState(BaseModel):
    """Retained on ``/master_template/{id}/state`` — late subscribers
    see the current truth without waiting for the next event."""
    enabled: bool
    tick_count: int
    message: str


class ExampleEventPayload(BaseModel):
    """Published on ``/master_template/{id}/example_event`` by
    ``do_thing`` — this is the pattern to copy for any topic your
    service publishes beyond ``state``: declare a model, add it to
    ``topic_schemas``, construct + ``.model_dump()`` it at the publish
    call site instead of a bare dict literal."""
    count: int = Field(description="Echoes the do_thing call's count arg")
    message: str = Field(description="The config's example_message at publish time")


class MasterTemplateService(Service):
    """See ``framework/service.py`` for the full base-class surface.
    The interesting hooks are:

      * ``on_start``        — called once after config is hydrated
      * ``on_stop``         — called on graceful shutdown
      * @service_method     — decorate methods to expose them on /control
      * ``self.publish``    — publish to ``/<type>/<id>/<suffix>``
      * ``self.config``     — the typed config instance
    """

    config_class = MasterTemplateConfig
    state_schema = MasterTemplateState
    topic_schemas = {"example_event": ExampleEventPayload}
    # Always-on topics — surfaced in the Composer's Topics tab + the
    # Topology page so users see what instances of this type broadcast.
    publishes = ["state", "example_event"]
    _controller_task: Optional[asyncio.Task]

    async def on_start(self) -> None:
        """First thing called after config is hydrated. Set up any
        resources here; the bus is already wired by the time this
        runs. Wiring the /control dispatcher is YOUR job, not the
        framework's — forgetting this line is the most common reason a
        new service's @service_method actions silently never fire."""
        logger.info(
            "master_template %s starting — message=%r tick_count=%d enabled=%s",
            self.proxy_id,
            self.config.example_message,
            self.config.tick_count,
            self.config.enabled,
        )
        self._publish_state()
        self._controller_task = asyncio.create_task(self.run_control_loop())

    async def on_stop(self) -> None:
        """Release anything ``on_start`` allocated. Best-effort; the
        framework will cancel/await outstanding tasks during shutdown."""
        logger.info("master_template %s stopping", self.proxy_id)
        if self._controller_task is not None and not self._controller_task.done():
            self._controller_task.cancel()
            try:
                await self._controller_task
            except (asyncio.CancelledError, Exception):
                pass

    # ─── @service_method actions ────────────────────────────────────
    # Decorate any handler with @service_method("<name>") to make it
    # available on /control. The framework's control loop dispatches by
    # the message's ``action`` field, handles reply_to round-trips, and
    # logs unknown actions.

    @service_method("do_thing", publishes=["example_event"])
    def do_thing(self, count: int = 1) -> Dict[str, Any]:
        """Example action. Takes a kwarg ``count`` (its JSON Schema is
        derived from this signature, not hand-written) and publishes an
        example event demonstrating ``topic_schemas`` end-to-end."""
        logger.info("master_template %s: do_thing count=%d", self.proxy_id, count)
        event = ExampleEventPayload(count=count, message=self.config.example_message)
        self.publish("example_event", event.model_dump())
        return {"did_thing": True, "count": count, "message": self.config.example_message}

    @service_method("set_message")
    def set_message(self, message: str) -> Dict[str, Any]:
        """Mutate + persist config. ``merge_dict`` re-validates against
        ``config_class`` and returns a new instance; ``save_config``
        (auto-mounted, see the module docstring) writes it to the active
        config set's yml so it survives restart. This is the real
        equivalent of what a previous version of this file called
        ``self.update_config(...)`` — that method never existed."""
        self.config = self.config.merge_dict({"example_message": str(message)})
        self.save_config()
        self._publish_state()
        return {"example_message": self.config.example_message}

    # ─── internals ────────────────────────────────────────────────────

    def _publish_state(self) -> None:
        """One retained state message. Every service that has an
        at-a-glance status writes a private helper like this one — the
        framework doesn't provide a generic ``publish_state()``."""
        state = MasterTemplateState(
            enabled=self.config.enabled,
            tick_count=self.config.tick_count,
            message=self.config.example_message,
        )
        self.publish("state", state.model_dump(), retained=True)
