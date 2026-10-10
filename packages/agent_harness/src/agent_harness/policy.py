"""Run policy: data residency, network, time/turn limits.

Residency is a hard gate checked before any agent is started: a
`local_only` policy refuses every runtime whose capabilities say data can
leave the host, whatever else the configuration says.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from agent_harness.runtime import AgentRuntime


class PolicyError(RuntimeError):
    pass


@dataclass
class Policy:
    residency: Literal["local_only", "cloud_ok"] = "local_only"
    network: Literal["deny", "allow"] = "deny"
    max_minutes: float = 30
    max_turns: Optional[int] = None
    # With network="allow": optional domain allowlist for web fetches
    # (empty = any domain). Web search itself can't be domain-limited.
    allowed_domains: list[str] = field(default_factory=list)
    # MCP servers declared by the workflow (`mcp:`) are usable only with
    # mcp="allow" — they reach live systems, so it's opt-in like network.
    mcp: Literal["deny", "allow"] = "deny"

    @classmethod
    def from_dict(cls, *layers: Optional[dict[str, Any]]) -> "Policy":
        """Merge dict layers left to right (workflow defaults, then caller
        overrides). Unknown keys are rejected so typos can't silently
        weaken a policy."""
        known = {f.name for f in fields(cls)}
        merged: dict[str, Any] = {}
        for layer in layers:
            for key, value in (layer or {}).items():
                if key not in known:
                    raise PolicyError(f"unknown policy key {key!r}")
                if value is not None:
                    merged[key] = value
        policy = cls(**merged)
        if policy.residency not in ("local_only", "cloud_ok"):
            raise PolicyError(f"invalid residency {policy.residency!r}")
        if policy.network not in ("deny", "allow"):
            raise PolicyError(f"invalid network {policy.network!r}")
        if policy.mcp not in ("deny", "allow"):
            raise PolicyError(f"invalid mcp {policy.mcp!r}")
        return policy

    def check_runtime(self, runtime: "AgentRuntime") -> None:
        if self.residency == "local_only" and runtime.capabilities.cloud:
            raise PolicyError(
                f"policy residency=local_only refuses agent {runtime.name!r}: it sends "
                f"data off-host. Set residency=cloud_ok to allow it explicitly."
            )

    def check_mcp(self, runtime: "AgentRuntime", servers: list) -> None:
        """A workflow that declares MCP servers runs only with mcp=allow and
        on an agent that can use them — refused before anything is staged."""
        if not servers:
            return
        names = ", ".join(s.name for s in servers)
        if self.mcp != "allow":
            raise PolicyError(f"workflow declares MCP servers ({names}) but policy mcp=deny; "
                              f"set mcp=allow to let the agent reach them")
        if not runtime.capabilities.mcp:
            raise PolicyError(f"agent {runtime.name!r} can't use MCP servers ({names})")
