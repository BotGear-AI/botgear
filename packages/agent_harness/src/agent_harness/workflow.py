"""Load WORKFLOW.md contracts and SKILL.md folders from search paths.

A workflow is a folder `<name>/WORKFLOW.md`: YAML frontmatter (the machine
contract: inputs, skills, outputs, checks, policy, stages) plus a markdown
body (the goal and success criteria, in prose). It describes WHAT and WHAT
COUNTS AS SUCCESS, never the execution algorithm; the agent decides how.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from agent_harness.mcp_servers import McpServer, McpSpecError, parse_mcp

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class WorkflowError(ValueError):
    pass


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError("frontmatter must be a mapping")
    return data, m.group(2)


@dataclass
class InputSpec:
    name: str
    type: str = "string"          # "path" | "string"
    required: bool = True
    readonly: bool = True
    description: str = ""


@dataclass
class Workflow:
    name: str
    description: str
    path: Path                     # the workflow folder
    body: str
    inputs: dict[str, InputSpec] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)            # must exist for the run to pass
    optional_outputs: list[str] = field(default_factory=list)   # asked for, not required
    deliverables: list[str] = field(default_factory=list)       # subset of all outputs meant for humans
    checks: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    stages: list[str] = field(default_factory=list)
    context_commands: list[dict[str, str]] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)   # env var names the caller may supply
    mcp: dict[str, McpServer] = field(default_factory=dict)  # MCP servers the agent may call (policy mcp: allow)

    @property
    def all_outputs(self) -> list[str]:
        return [*self.outputs, *self.optional_outputs]


def find_named_dir(name: str, search_paths: Iterable[Path], marker: str) -> Path:
    for base in search_paths:
        candidate = Path(base) / name
        if (candidate / marker).is_file():
            return candidate
    raise WorkflowError(f"{marker} for {name!r} not found in: {[str(p) for p in search_paths]}")


def load_workflow(name: str, search_paths: Iterable[Path]) -> Workflow:
    search_paths = [Path(p) for p in search_paths]
    wf_dir = find_named_dir(name, search_paths, "WORKFLOW.md")
    meta, body = parse_frontmatter((wf_dir / "WORKFLOW.md").read_text(encoding="utf-8"))
    if meta.get("name") != name:
        raise WorkflowError(f"{wf_dir}/WORKFLOW.md: frontmatter name must equal folder name {name!r}")
    if not meta.get("description"):
        raise WorkflowError(f"{wf_dir}/WORKFLOW.md: description is required")

    inputs: dict[str, InputSpec] = {}
    for input_name, spec in (meta.get("inputs") or {}).items():
        spec = spec or {}
        inputs[input_name] = InputSpec(
            name=input_name,
            type=spec.get("type", "string"),
            required=spec.get("required", True),
            readonly=spec.get("readonly", True),
            description=spec.get("description", ""),
        )

    outputs = list(meta.get("outputs") or [])
    optional_outputs = list(meta.get("optional_outputs") or [])
    _check_output_paths(outputs + optional_outputs)
    deliverables = list(meta.get("deliverables") or outputs + optional_outputs)
    undeclared = set(deliverables) - set(outputs) - set(optional_outputs)
    if undeclared:
        raise WorkflowError(f"deliverables {sorted(undeclared)} are not declared outputs")

    secrets = list(meta.get("secrets") or [])
    try:
        mcp = parse_mcp(meta.get("mcp"), secrets, f"{wf_dir}/WORKFLOW.md")
    except McpSpecError as exc:
        raise WorkflowError(str(exc)) from exc

    return Workflow(
        name=name,
        description=str(meta["description"]).strip(),
        path=wf_dir,
        body=body.strip(),
        inputs=inputs,
        skills=list(meta.get("skills") or []),
        outputs=outputs,
        optional_outputs=optional_outputs,
        deliverables=deliverables,
        checks=list(meta.get("checks") or []),
        policy=dict(meta.get("policy") or {}),
        stages=list(meta.get("stages") or []),
        context_commands=list(meta.get("context_commands") or []),
        secrets=secrets,
        mcp=mcp,
    )


_INPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_output_paths(paths: Iterable[str]) -> None:
    for output in paths:
        if not str(output).startswith("output/") or ".." in Path(output).parts:
            raise WorkflowError(f"output {output!r} must be under output/ (the only writable dir)")


def skill_workflow(
    skill: str,
    skill_dir: Path,
    *,
    inputs: dict[str, Any],
    outputs: list[str],
    optional_outputs: Optional[list[str]] = None,
    instructions: str = "",
    checks: Optional[list[str]] = None,
    policy: Optional[dict[str, Any]] = None,
    secrets: Optional[list[str]] = None,
    extra_skills: Optional[list[str]] = None,
) -> Workflow:
    """An in-memory workflow for running ONE skill as the whole task (no
    WORKFLOW.md needed). Every input whose value is an existing path becomes
    a read-only path input under inputs/<name>; others are plain values.
    `extra_skills` are snapshotted next to it (for skills that run another
    skill's scripts via `../<other-skill>/scripts/...`)."""
    optional_outputs = list(optional_outputs or [])
    _check_output_paths([*outputs, *optional_outputs])
    specs = {}
    for name, value in inputs.items():
        if not _INPUT_NAME.match(name):
            raise WorkflowError(f"invalid input name {name!r}")
        is_path = isinstance(value, Path) or (isinstance(value, str) and value.startswith("/") and Path(value).exists())
        specs[name] = InputSpec(name=name, type="path" if is_path else "string", required=True)
    body = (
        f"Run the `{skill}` skill: read `.claude/skills/{skill}/SKILL.md` and follow it exactly. "
        "Where the skill refers to its own files (`scripts/...`, `reference.md`, `assets/...`), they are "
        f"under `.claude/skills/{skill}/`. Its input files are under `inputs/` and every file it tells "
        "you to write goes under `output/`."
    )
    if instructions:
        body += "\n\n" + instructions.strip()
    return Workflow(
        name=skill, description=f"Run the {skill} skill.", path=Path(skill_dir), body=body,
        inputs=specs, skills=[skill, *[s for s in (extra_skills or []) if s != skill]],
        outputs=list(outputs), optional_outputs=optional_outputs, deliverables=[*outputs, *optional_outputs],
        checks=list(checks or []), policy=dict(policy or {}), secrets=list(secrets or []),
    )


def resolve_skills(names: Iterable[str], search_paths: Iterable[Path]) -> dict[str, Path]:
    search_paths = [Path(p) for p in search_paths]
    resolved: dict[str, Path] = {}
    for name in names:
        skill_dir = find_named_dir(name, search_paths, "SKILL.md")
        meta, _ = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        if not meta.get("name") or not meta.get("description"):
            raise WorkflowError(f"{skill_dir}/SKILL.md is missing name/description frontmatter")
        resolved[name] = skill_dir
    return resolved


def resolve_inputs(workflow: Workflow, provided: dict[str, Any]) -> dict[str, Any]:
    unknown = set(provided) - set(workflow.inputs)
    if unknown:
        raise WorkflowError(f"unknown inputs for {workflow.name!r}: {sorted(unknown)}")
    resolved: dict[str, Any] = {}
    for name, spec in workflow.inputs.items():
        if name not in provided or provided[name] in (None, ""):
            if spec.required:
                raise WorkflowError(f"missing required input {name!r}")
            continue
        value = provided[name]
        if spec.type == "path":
            value = Path(value).expanduser().resolve()
            if not value.exists():
                raise WorkflowError(f"input {name!r}: path does not exist: {value}")
        resolved[name] = value
    return resolved


def substitute(text: str, values: dict[str, Any]) -> str:
    """Replace {key} placeholders without str.format's brace fragility
    (prompt/markdown text routinely contains literal braces)."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def resolve_mcp(workflow: Workflow, placeholders: dict[str, Any]) -> list[McpServer]:
    """The workflow's MCP servers with ``{input.*}`` / ``{workspace}`` etc.
    substituted in url and ca_cert (headers keep their ``{secret.*}`` refs).
    A relative ca_cert (a path input renders as ``inputs/<name>``) is made
    absolute against the workspace, so it means the same file wherever it's
    read from."""
    out = []
    workspace = placeholders.get("workspace")
    for server in workflow.mcp.values():
        ca_cert = substitute(server.ca_cert, placeholders) if server.ca_cert else None
        if ca_cert and workspace and not Path(ca_cert).is_absolute():
            ca_cert = str((Path(workspace) / ca_cert).resolve())
        out.append(McpServer(
            name=server.name, url=substitute(server.url, placeholders), headers=dict(server.headers),
            tools=list(server.tools), description=server.description, ca_cert=ca_cert,
        ))
    return out


def self_check_commands(workflow: Workflow, placeholders: dict[str, Any]) -> list[str]:
    """The workflow's checks as the agent runs them: from the workspace, against
    its snapshot of the workflow folder (the harness re-runs the originals)."""
    return [substitute(cmd, {**placeholders, "workflow_dir": "workflow"}) for cmd in workflow.checks]


def render_task(
    workflow: Workflow,
    inputs: dict[str, Any],
    skills: dict[str, Path],
    placeholders: dict[str, Any],
    *,
    tools: bool,
    self_check: bool = True,
) -> str:
    """Render task.md: the workflow body plus the harness contract (inputs,
    skills, outputs, rules). `tools=False` phrases it for runtimes that can't
    read files themselves (context is supplied inline instead)."""
    lines = [f"# Task: {workflow.name}", "", workflow.description, "", substitute(workflow.body, placeholders), ""]

    lines += ["## Inputs", ""]
    for name, spec in workflow.inputs.items():
        if name not in inputs:
            continue
        if spec.type == "path":
            access = "read-only" if spec.readonly else "read-write"
            lines.append(f"- `{name}`: `inputs/{name}` ({access}){' - ' + spec.description if spec.description else ''}")
        else:
            lines.append(f"- `{name}`: `{inputs[name]}`{' - ' + spec.description if spec.description else ''}")
    lines.append("")

    if skills:
        lines += ["## Skills", ""]
        if tools:
            lines.append("These skills are installed in this workspace; read each SKILL.md before using it:")
        else:
            lines.append("These skills' instructions are included below; their scripts were already run for you:")
        for name in skills:
            lines.append(f"- `{name}`: `.claude/skills/{name}/SKILL.md`")
        lines.append("")

    if tools and workflow.mcp:
        lines += ["## Live tools (MCP)", "",
                  "These MCP servers are connected; call their tools as `mcp__<server>__<tool>`. "
                  "They act on a LIVE system: read before you change anything, and only change "
                  "what the task asks for.", ""]
        for server in workflow.mcp.values():
            allowed = "any of its tools" if server.all_tools else ", ".join(f"`{t}`" for t in server.tools)
            lines.append(f"- `{server.name}`{' - ' + server.description if server.description else ''} "
                         f"(allowed: {allowed})")
        lines.append("")

    if workflow.stages:
        lines += ["## Stages", "", "Work through these stages in order. Write each stage's "
                  "artifacts to `output/<stage>/` before starting the next:", ""]
        lines += [f"{i}. `{stage}`" for i, stage in enumerate(workflow.stages, 1)]
        lines.append("")

    if tools and workflow.checks and self_check:
        lines += ["## Self-check", "",
                  "These checks run automatically after you finish, and only their result counts. "
                  "Run them yourself before finishing and fix whatever they report:", ""]
        lines += [f"- `{cmd}`" for cmd in self_check_commands(workflow, placeholders)]
        lines.append("")

    lines += ["## Required outputs", ""]
    lines += [f"- `{path}`" for path in workflow.outputs]
    if workflow.optional_outputs:
        lines += ["", "## Also write, if you can", ""]
        lines += [f"- `{path}`" for path in workflow.optional_outputs]
    lines += [
        "",
        "## Rules",
        "",
        "- Write files ONLY under `output/`. Everything else in this workspace is read-only.",
        "- Base every statement on data you actually read; if something is missing, say so instead of guessing.",
    ]
    if tools:
        python = placeholders.get("python", "python3")
        lines += [
            f"- `output/` already exists. Run skill scripts as ONE plain command each, exactly in the form "
            f"`{python} .claude/skills/<skill>/scripts/<script>.py <args>`"
            f"{' (or a self-check command above)' if workflow.checks and self_check else ''} "
            f"from the workspace root. Only those forms are permitted: `cd`, loops, pipes, `&&`, variables and `python -c` are denied, so run the "
            f"scripts one by one. Use the Read/Grep/Glob tools for everything else"
            f"{', and the MCP tools listed above for the live system' if workflow.mcp else ''}.",
        ]
    lines += [
        "- Finish with a one-paragraph summary of what you produced.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def read_optional(path: Optional[Path]) -> str:
    return Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
