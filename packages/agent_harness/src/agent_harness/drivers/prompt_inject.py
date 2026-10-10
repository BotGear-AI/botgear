"""prompt_inject driver: ONE model call, no tools.

For runtimes that can't use tools. The harness does the reading for them:
the workflow's `context_commands` run here (or, if it declares none, the
text files under inputs/ are inlined), their output goes into the prompt
with the skills' SKILL.md text, and the model returns every output file in
one JSON object, which this driver writes into output/.

Providers (config `provider`):
  openai     (default) any OpenAI-compatible chat endpoint: Ollama / vLLM /
             LM Studio / llama.cpp locally, or a cloud API. Needs base_url.
             With a loopback/private base_url this is the only driver that
             works under `residency: local_only`. Needs agent_harness[openai].
  anthropic  the Anthropic Messages API through the official SDK. Needs
             agent_harness[anthropic]; base_url optional.

Config: provider, base_url, api_key, model, timeout_s, max_tokens.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from agent_harness.events import AgentEvent
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask, Capabilities

_CONTEXT_LIMIT = 60_000   # chars per context command / input file, keeps prompts bounded
_INPUTS_LIMIT = 200_000   # chars of inlined input files in total
_TEXT_SUFFIXES = {".json", ".md", ".txt", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".html"}
_PROVIDERS = ("openai", "anthropic")


def is_local_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


class PromptInjectDriver:
    name = "prompt_inject"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.provider = config.get("provider") or "openai"
        if self.provider not in _PROVIDERS:
            raise ValueError(f"prompt_inject: unknown provider {self.provider!r}; use one of {_PROVIDERS}")
        local = self.provider == "openai" and is_local_url(config.get("base_url", ""))
        self.capabilities = Capabilities(tools=False, streaming=False, resume=False, cloud=not local)

    def build_messages(self, workspace: Path, task: AgentTask) -> list[dict[str, str]]:
        system = [task.system_context] if task.system_context else []
        for name, skill_dir in task.skills.items():
            system.append(f"## Skill: {name}\n\n" + (skill_dir / "SKILL.md").read_text(encoding="utf-8"))
            reference = skill_dir / "reference.md"
            if reference.is_file():
                system.append(f"### {name}/reference.md\n\n" + reference.read_text(encoding="utf-8"))
        system.append(
            "You cannot read files or run tools. The context you need is in the user message. "
            "Respond with ONE JSON object and nothing else (no code fences). Its keys must be exactly "
            f"these output paths: {json.dumps(task.outputs)}. Each value is that file's full content: "
            "a string for text/markdown files, an object for .json files."
        )

        context = [] if task.context_commands else _inline_inputs(workspace / "inputs")
        for item in task.context_commands:
            title, command = item.get("title", item.get("command", "")), item.get("command", "")
            try:
                proc = subprocess.run(shlex.split(command), cwd=workspace, capture_output=True,
                                      text=True, timeout=300)
                text = proc.stdout if proc.returncode == 0 else f"(command failed: {proc.stderr[-500:]})"
            except (OSError, subprocess.TimeoutExpired) as exc:
                text = f"(command failed: {exc})"
            context.append(f"## {title}\n\n{text[:_CONTEXT_LIMIT]}")
        user = task.prompt + "\n\n# Context\n\n" + ("\n\n".join(context) or "(none)")
        return [{"role": "system", "content": "\n\n".join(system)}, {"role": "user", "content": user}]

    def cancel(self) -> None:
        """One blocking call: nothing to interrupt (the harness still records
        the run as cancelled)."""

    def run(self, workspace: Path, task: AgentTask, policy: Policy) -> Iterator[AgentEvent]:
        cfg = self.config
        if self.provider == "openai" and not cfg.get("base_url"):
            yield AgentEvent("error", self.name, {"message": "prompt_inject: base_url is not configured", "fatal": True})
            return
        messages = self.build_messages(workspace, task)
        yield AgentEvent("status", self.name, {"phase": "init", "model": cfg.get("model"), "provider": self.provider,
                                               "prompt_chars": sum(len(m["content"]) for m in messages)})
        timeout = cfg.get("timeout_s") or policy.max_minutes * 60
        try:
            complete = _complete_anthropic if self.provider == "anthropic" else _complete_openai
            content, usage = complete(cfg, messages, timeout)
        except ImportError as exc:
            yield AgentEvent("error", self.name, {"message": f"prompt_inject ({self.provider}) needs {exc.name}: "
                                                             f"install agent_harness[{self.provider}]", "fatal": True})
            return
        except Exception as exc:
            yield AgentEvent("error", self.name, {"message": f"{type(exc).__name__}: {exc}", "fatal": True})
            return

        if usage:
            yield AgentEvent("usage", self.name, usage)
        files = parse_files(content, task.outputs)
        if not files:
            yield AgentEvent("error", self.name, {"message": "response was not the requested JSON object; "
                                                             "raw response saved to output/raw_response.txt"})
            (workspace / "output" / "raw_response.txt").write_text(content, encoding="utf-8")
        for rel, value in files.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2), encoding="utf-8")
            yield AgentEvent("file_change", self.name, {"tool": "write", "path": rel})
        yield AgentEvent("completed", self.name, {"message": f"wrote {len(files)} of {len(task.outputs)} outputs"})


def _complete_openai(cfg: dict[str, Any], messages: list[dict[str, str]], timeout: float) -> tuple[str, dict]:
    import openai
    client = openai.OpenAI(base_url=cfg["base_url"], api_key=cfg.get("api_key") or "not-needed")
    resp = client.chat.completions.create(model=cfg.get("model"), messages=messages, timeout=timeout)
    content = (resp.choices[0].message.content or "") if resp.choices else ""
    usage = ({"input_tokens": resp.usage.prompt_tokens, "output_tokens": resp.usage.completion_tokens}
             if getattr(resp, "usage", None) else {})
    return content, usage


def _complete_anthropic(cfg: dict[str, Any], messages: list[dict[str, str]], timeout: float) -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic(**{k: cfg[k] for k in ("api_key", "base_url") if cfg.get(k)}, timeout=timeout)
    system, user = messages[0]["content"], messages[1]["content"]
    resp = client.messages.create(model=cfg.get("model"), max_tokens=int(cfg.get("max_tokens") or 16000),
                                  system=system, messages=[{"role": "user", "content": user}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined the request (stop_reason=refusal)")
    content = "".join(block.text for block in resp.content if block.type == "text")
    usage = {k: getattr(resp.usage, k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens")
             if isinstance(getattr(resp.usage, k, None), int)}
    return content, usage


def _inline_inputs(inputs_dir: Path) -> list[str]:
    """Text files under inputs/ (following the input symlinks) as prompt
    sections, for tool-less runs whose workflow declares no context_commands.
    Binary files are listed by name only; everything is size-capped."""
    if not inputs_dir.is_dir():
        return []
    sections, budget = [], _INPUTS_LIMIT
    files = sorted(Path(d) / f for d, _, names in os.walk(inputs_dir, followlinks=True) for f in names)
    for path in files:
        rel = path.relative_to(inputs_dir.parent)
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            sections.append(f"## {rel}\n\n(binary file, not included)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")[:min(_CONTEXT_LIMIT, budget)]
        if not text:
            sections.append(f"## {rel}\n\n(omitted: input size limit reached)")
            continue
        budget -= len(text)
        sections.append(f"## {rel}\n\n{text}")
    return sections


def parse_files(content: str, outputs: list[str]) -> dict[str, Any]:
    """Extract {output_path: content} from the model's reply, tolerating code
    fences. Only declared output paths under output/ are accepted."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in outputs and str(k).startswith("output/")}
