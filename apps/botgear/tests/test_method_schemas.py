# unmanaged
"""Method argument schemas reach agents on BOTH transports.

Agents (brain, the MCP gateway) build tools from each service's advertised
``args_schema``. In-process services publish theirs in ``meta``; subprocess
services in their methods manifest (``SubprocessService._methods_payload``),
the only source the backend has since it can't import their classes. Rule
on both paths: a hand-written ``@service_method(args_schema=...)`` wins,
otherwise the schema is derived from the method's signature."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from rlx_bus import ServiceConfig, SubprocessService, service_method

from botgear.framework import Service, service_method as fw_service_method
from botgear.framework.service import ServiceMetadata

HAND_WRITTEN = {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}


class _Sub(SubprocessService):
    type_name = "thing"

    @service_method("speak")
    async def m_speak(self, text: str = "", voice: Optional[str] = None, wait: bool = False) -> dict:
        return {}

    @service_method("power_set", args_schema=HAND_WRITTEN)
    async def m_power_set(self, **kwargs) -> dict:
        return {}

    @service_method("broken")
    async def m_broken(self, x: "NoSuchType") -> dict:  # noqa: F821 — unresolvable annotation
        return {}


def _payload(svc) -> dict:
    return {m["name"]: m for m in svc._methods_payload()}


def test_subprocess_manifest_derives_schemas_from_signatures():
    methods = _payload(_Sub("thing-1", SimpleNamespace()))
    speak = methods["speak"]["args_schema"]
    assert set(speak["properties"]) == {"text", "voice", "wait"}
    assert speak["properties"]["wait"] == {"type": "boolean", "default": False}
    assert "required" not in speak                                     # all have defaults
    assert methods["power_set"]["args_schema"] == HAND_WRITTEN          # hand-written wins
    assert methods["broken"]["args_schema"] is not None                 # never breaks the manifest


def test_real_speech_speak_is_fully_described():
    from rlx_audio import SpeechServiceBase

    class _Speech(SpeechServiceBase):
        source = "local"

        def _synthesize(self, text, out_path, opts):
            pass

    schema = {m["name"]: m for m in _Speech("mouth", SimpleNamespace())._methods_payload()}["speak"]["args_schema"]
    assert {"text", "voice", "interrupt", "wait", "wait_timeout_s"} <= set(schema["properties"])
    assert schema["properties"]["text"]["type"] == "string"


class _InProc(Service):
    config_class = ServiceConfig

    @fw_service_method("speak")
    async def m_speak(self, text: str = "") -> dict:
        return {}

    @fw_service_method("power_set", args_schema=HAND_WRITTEN)
    async def m_power_set(self, on: bool, extra: int = 0) -> dict:
        return {}


def test_in_process_meta_prefers_hand_written_schema():
    svc = _InProc(ServiceMetadata(proxy_id="t-1", service_meta_id="t@1.0.0", type_name="t",
                                  type_version="1.0.0", tags=[], singleton=False), {})
    methods = {m["name"]: m for m in svc._build_meta_payload()["methods"]}
    assert methods["power_set"]["args_schema"] == HAND_WRITTEN          # was ignored before
    assert set(methods["speak"]["args_schema"]["properties"]) == {"text"}


def test_gateway_sees_the_derived_schema():
    from botgear.agent_surface import add_meta
    from botgear.mcp_gateway import tools as mcp_tools
    catalog: dict = {}
    add_meta(catalog, {"topics": {"control": "/thing/thing-1/control"},
                       "methods": _Sub("thing-1", SimpleNamespace())._methods_payload()})
    tool = mcp_tools.to_mcp_tool("thing-1__speak", catalog["/thing/thing-1/control::speak"])
    assert set(tool.input_schema["properties"]) == {"text", "voice", "wait"}


def test_kwargs_methods_advertise_an_open_schema():
    from rlx_bus.introspect import args_schema_for

    async def m_set(self, mode: str, **kwargs):
        return {}
    schema = args_schema_for(m_set)
    assert schema["additionalProperties"] is True and schema["required"] == ["mode"]

    async def m_plain(self, mode: str):
        return {}
    assert args_schema_for(m_plain)["additionalProperties"] is False
