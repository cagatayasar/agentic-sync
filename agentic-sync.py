from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KNOWN_TARGETS = ("claude", "codex", "opencode", "cursor")
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
LINE_DIRECTIVE_RE = re.compile(r"\s*\[agentic-sync:(?P<selector>[^\]]+)\]\s*$")
BLOCK_START_RE = re.compile(r"^\s*\[agentic-sync-start:(?P<selector>[^\]]+)\]\s*$")
BLOCK_END_RE = re.compile(r"^\s*\[agentic-sync-end\]\s*$")
FRONTMATTER_RE = re.compile(
    r"^(---\r?\n(?P<frontmatter>[\s\S]*?)\r?\n---)(?:\r?\n(?P<body>[\s\S]*))?$"
)
TARGET_LAYOUTS: dict[str, dict[str, str]] = {
    "claude": {
        "doc_destination": "CLAUDE.md",
        "commands_destination": ".claude/commands",
        "agents_destination": ".claude/agents",
        "skills_destination": ".claude/skills",
    },
    "codex": {
        "doc_destination": "AGENTS.md",
        "agents_destination": ".codex/agents",
        "skills_destination": ".agents/skills",
    },
    "opencode": {
        "doc_destination": "AGENTS.md",
        "commands_destination": ".opencode/commands",
        "agents_destination": ".opencode/agents",
        "skills_destination": ".opencode/skills",
    },
    "cursor": {
        "doc_destination": "AGENTS.md",
        "commands_destination": ".cursor/commands",
    },
}


@dataclass
class Request:
    mode: str
    destination: str
    source: str
    source_path: Path | None
    content: str | None
    hash_value: str
    kind: str
    targets: list[str]
    compiled: bool = False


def normalize_targets(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in str(value).split(","):
            normalized = item.strip().lower()
            if not normalized:
                continue
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_relative(path: Path, project_root: Path) -> str:
    return os.path.relpath(path, project_root).replace("\\", "/")


def split_markdown_document(text: str) -> tuple[str | None, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    return match.group("frontmatter"), match.group("body") or ""


def parse_yaml_scalar(value: str) -> Any:
    trimmed = value.strip()
    if trimmed == "":
        return ""
    if trimmed.startswith("'") and trimmed.endswith("'"):
        return trimmed[1:-1].replace("''", "'")
    if trimmed.startswith('"') and trimmed.endswith('"'):
        return (
            trimmed[1:-1]
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
        )
    if trimmed == "true":
        return True
    if trimmed == "false":
        return False
    if trimmed in {"null", "~"}:
        return None
    if trimmed.startswith("[") and trimmed.endswith("]"):
        inner = trimmed[1:-1].strip()
        if not inner:
            return []
        return [parse_yaml_scalar(part) for part in re.split(r"\s*,\s*", inner)]
    if re.fullmatch(r"-?\d+", trimmed):
        return int(trimmed)
    if re.fullmatch(r"-?\d+\.\d+", trimmed):
        return float(trimmed)
    return trimmed


def next_yaml_index(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        return index
    return None


def parse_yaml_block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    current = next_yaml_index(lines, start)
    if current is None:
        return OrderedDict(), len(lines)

    line = lines[current]
    line_indent = len(line) - len(line.lstrip(" "))
    if line_indent < indent:
        return OrderedDict(), current

    stripped = line.strip()
    if stripped.startswith("- "):
        items: list[Any] = []
        while current < len(lines):
            line = lines[current]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                current += 1
                continue
            line_indent = len(line) - len(line.lstrip(" "))
            if line_indent < indent:
                break
            if line_indent != indent or not stripped.startswith("- "):
                raise ValueError(f"Unsupported YAML near '{line}'.")
            item_text = stripped[2:].strip()
            if item_text == "":
                nested, current = parse_yaml_block(lines, current + 1, indent + 2)
                items.append(nested)
            else:
                items.append(parse_yaml_scalar(item_text))
                current += 1
        return items, current

    mapping: OrderedDict[str, Any] = OrderedDict()
    while current < len(lines):
        line = lines[current]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            current += 1
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent < indent:
            break
        if line_indent != indent or ":" not in stripped:
            raise ValueError(f"Unsupported YAML near '{line}'.")
        key, rest = stripped.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest:
            mapping[key] = parse_yaml_scalar(rest)
            current += 1
            continue
        next_index = next_yaml_index(lines, current + 1)
        if next_index is None:
            mapping[key] = ""
            current += 1
            continue
        next_line = lines[next_index]
        next_indent = len(next_line) - len(next_line.lstrip(" "))
        if next_indent <= line_indent:
            mapping[key] = ""
            current += 1
            continue
        nested, current = parse_yaml_block(lines, current + 1, line_indent + 2)
        mapping[key] = nested
    return mapping, current


def load_yaml(raw: str | None) -> OrderedDict[str, Any]:
    if not raw or not raw.strip():
        return OrderedDict()
    value, _ = parse_yaml_block(raw.splitlines(), 0, 0)
    return normalize_yaml_value(value)


def normalize_yaml_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, OrderedDict):
        return OrderedDict((str(k), normalize_yaml_value(v)) for k, v in value.items())
    if isinstance(value, dict):
        return OrderedDict((str(k), normalize_yaml_value(v)) for k, v in value.items())
    if isinstance(value, list):
        return [normalize_yaml_value(item) for item in value]
    return value


def dump_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return "''"
    if (
        text == text.strip()
        and "\n" not in text
        and "\r" not in text
        and "\t" not in text
        and text[0] not in "-?:,[]{}#&*!|>'\"%@`"
        and not re.fullmatch(r"true|false|null|~", text, re.IGNORECASE)
        and not re.fullmatch(r"-?\d+", text)
        and not re.fullmatch(r"-?\d+\.\d+", text)
        and ": " not in text
        and not text.endswith(":")
        and " #" not in text
    ):
        return text
    return "'" + text.replace("'", "''") + "'"


def dump_yaml(value: OrderedDict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(dump_yaml(OrderedDict(item), indent + 2))
        elif isinstance(item, list):
            if not item:
                lines.append(f"{prefix}{key}: []")
                continue
            lines.append(f"{prefix}{key}:")
            for list_item in item:
                if isinstance(list_item, dict):
                    lines.append(f"{prefix}  -")
                    lines.append(dump_yaml(OrderedDict(list_item), indent + 4))
                else:
                    lines.append(f"{prefix}  - {dump_yaml_scalar(list_item)}")
        else:
            lines.append(f"{prefix}{key}: {dump_yaml_scalar(item)}")
    return "\n".join(lines)


def parse_selector(selector: str) -> tuple[bool, list[str]]:
    selector = selector.strip()
    is_except = False
    if selector.lower().startswith("except="):
        is_except = True
        selector = selector[7:]
    targets = normalize_targets([selector])
    if not targets:
        raise ValueError("Directive target list cannot be empty.")
    unknown = [target for target in targets if target not in KNOWN_TARGETS]
    if unknown:
        raise ValueError(f"Unknown directive targets: {', '.join(unknown)}.")
    return is_except, targets


def selector_matches(selector: tuple[bool, list[str]], target: str) -> bool:
    is_except, targets = selector
    matched = target in targets
    return not matched if is_except else matched


def render_directives(text: str, target: str) -> str:
    lines = re.split(r"\r?\n", text)
    result: list[str] = []
    active_block: tuple[bool, list[str]] | None = None

    for line in lines:
        if BLOCK_END_RE.match(line):
            if active_block is None:
                raise ValueError(
                    "Found [agentic-sync-end] without a matching [agentic-sync-start:...]."
                )
            active_block = None
            continue

        block_match = BLOCK_START_RE.match(line)
        if block_match:
            if active_block is not None:
                raise ValueError(
                    "Nested [agentic-sync-start:...] blocks are not supported."
                )
            active_block = parse_selector(block_match.group("selector"))
            continue

        include = (
            True if active_block is None else selector_matches(active_block, target)
        )
        clean_line = line
        line_match = LINE_DIRECTIVE_RE.search(line)
        if line_match:
            include = include and selector_matches(
                parse_selector(line_match.group("selector")), target
            )
            clean_line = line[: line_match.start()].rstrip()
        if include:
            result.append(clean_line)

    if active_block is not None:
        raise ValueError(
            "Found [agentic-sync-start:...] without a matching [agentic-sync-end]."
        )

    return "\n".join(result)


def render_markdown(text: str, target: str) -> str:
    frontmatter, body = split_markdown_document(text)
    rendered_frontmatter = (
        None if frontmatter is None else render_directives(frontmatter, target).strip()
    )
    rendered_body = render_directives(body, target)
    if rendered_frontmatter is None:
        return rendered_body
    if not rendered_frontmatter:
        return rendered_body
    if rendered_body:
        return f"---\n{rendered_frontmatter}\n---\n{rendered_body}"
    return f"---\n{rendered_frontmatter}\n---"


def render_frontmatter(raw: str | None, target: str) -> OrderedDict[str, Any]:
    if raw is None:
        return OrderedDict()
    rendered = render_directives(raw, target).strip()
    if not rendered:
        return OrderedDict()
    return load_yaml(rendered)


def markdown_with_frontmatter(frontmatter: OrderedDict[str, Any], body: str) -> str:
    if not frontmatter:
        return body
    dumped = dump_yaml(frontmatter)
    if body:
        return f"---\n{dumped}\n---\n{body}"
    return f"---\n{dumped}\n---"


def new_text_request(
    destination: str,
    source: str,
    content: str,
    kind: str,
    targets: list[str],
    compiled: bool = False,
) -> Request:
    return Request(
        mode="text",
        destination=destination,
        source=source,
        source_path=None,
        content=content,
        hash_value=sha256_text(content),
        kind=kind,
        targets=normalize_targets(targets),
        compiled=compiled,
    )


def new_copy_request(
    destination: str, source: str, source_path: Path, kind: str, targets: list[str]
) -> Request:
    return Request(
        mode="copy",
        destination=destination,
        source=source,
        source_path=source_path,
        content=None,
        hash_value=sha256_bytes(source_path.read_bytes()),
        kind=kind,
        targets=normalize_targets(targets),
        compiled=False,
    )


def format_toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_toml_multiline(value: str) -> str:
    return '"""\n' + value.replace('"""', '\\"\\"\\"') + '\n"""'


def agent_to_codex_toml(frontmatter: OrderedDict[str, Any], body: str) -> str:
    if "name" not in frontmatter or "description" not in frontmatter:
        raise ValueError(
            "Canonical agent frontmatter must include 'name' and 'description'."
        )
    lines = [
        f"name = {format_toml_string(str(frontmatter['name']))}",
        f"description = {format_toml_string(str(frontmatter['description']))}",
    ]
    if frontmatter.get("model"):
        lines.append(f"model = {format_toml_string(str(frontmatter['model']))}")
    reasoning = frontmatter.get(
        "model_reasoning_effort", frontmatter.get("reasoning_effort")
    )
    if reasoning:
        lines.append(f"model_reasoning_effort = {format_toml_string(str(reasoning))}")
    if frontmatter.get("sandbox_mode"):
        lines.append(
            f"sandbox_mode = {format_toml_string(str(frontmatter['sandbox_mode']))}"
        )
    if frontmatter.get("nickname_candidates"):
        nicknames = ", ".join(
            format_toml_string(str(item))
            for item in list(frontmatter["nickname_candidates"])
        )
        lines.append(f"nickname_candidates = [{nicknames}]")
    lines.append(f"developer_instructions = {format_toml_multiline(body)}")
    return "\n".join(lines)


def merge_requests_by_destination(requests: list[Request]) -> list[Request]:
    grouped: dict[str, list[Request]] = defaultdict(list)
    for request in requests:
        grouped[request.destination].append(request)

    merged: list[Request] = []
    for destination in sorted(grouped):
        group = grouped[destination]
        first = group[0]
        for request in group[1:]:
            if request.mode != first.mode or request.hash_value != first.hash_value:
                targets = sorted({target for item in group for target in item.targets})
                raise ValueError(
                    f"Conflicting output for '{destination}' across targets: {', '.join(targets)}. "
                    "Shared output files must render identically."
                )
        merged.append(
            Request(
                mode=first.mode,
                destination=first.destination,
                source=first.source,
                source_path=first.source_path,
                content=first.content,
                hash_value=first.hash_value,
                kind=first.kind,
                targets=sorted({target for item in group for target in item.targets}),
                compiled=any(item.compiled for item in group),
            )
        )
    return merged


def target_destinations(target: str) -> list[str]:
    return list(TARGET_LAYOUTS[target].values())


def list_matching_files(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob(pattern) if path.is_file())


def list_skill_files(skills_root: Path) -> list[Path]:
    if not skills_root.exists():
        return []
    files: list[Path] = []
    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        files.extend(path for path in sorted(skill_dir.rglob("*")) if path.is_file())
    return files


def load_markdown_parts(path: Path) -> tuple[str | None, str]:
    return split_markdown_document(path.read_text(encoding="utf-8"))


def build_doc_request(
    project_root: Path, target: str, main_doc: Path
) -> tuple[Request | None, str | None]:
    source_path = main_doc
    notice = f"Skipped doc output for target '{target}' because '.agentic-sync/MAIN.md' does not exist."
    if not source_path.exists():
        return None, notice
    content = render_markdown(source_path.read_text(encoding="utf-8"), target)
    return (
        new_text_request(
            TARGET_LAYOUTS[target]["doc_destination"],
            project_relative(source_path, project_root),
            content,
            "doc",
            [target],
        ),
        None,
    )


def build_command_requests(
    project_root: Path, command_files: list[Path], target: str
) -> tuple[list[Request], list[str]]:
    if not command_files:
        return [], []
    if target == "codex":
        return [], [
            "Codex command syncing is skipped because Codex does not have a documented project-level custom command file layout."
        ]

    destination_root = TARGET_LAYOUTS[target].get("commands_destination")
    if destination_root is None:
        return [], []

    requests: list[Request] = []
    for path in command_files:
        frontmatter_raw, body = load_markdown_parts(path)
        rendered_body = render_directives(body, target)
        content = rendered_body
        if target != "cursor" and frontmatter_raw is not None:
            frontmatter = render_frontmatter(frontmatter_raw, target)
            content = markdown_with_frontmatter(frontmatter, rendered_body)
        requests.append(
            new_text_request(
                f"{destination_root}/{path.name}",
                project_relative(path, project_root),
                content,
                "command",
                [target],
            )
        )
    return requests, []


def build_agent_requests(
    project_root: Path, agent_files: list[Path], target: str
) -> list[Request]:
    destination_root = TARGET_LAYOUTS[target].get("agents_destination")
    if not agent_files or destination_root is None:
        return []

    requests: list[Request] = []
    for path in agent_files:
        frontmatter_raw, body = load_markdown_parts(path)
        if frontmatter_raw is None:
            raise ValueError(f"Agent file '{path.name}' must include YAML frontmatter.")
        frontmatter = render_frontmatter(frontmatter_raw, target)
        if "name" not in frontmatter or "description" not in frontmatter:
            raise ValueError(
                f"Agent file '{path.name}' must include 'name' and 'description' fields."
            )
        rendered_body = render_directives(body, target)
        source = project_relative(path, project_root)
        if target == "codex":
            requests.append(
                new_text_request(
                    f"{destination_root}/{path.stem}.toml",
                    source,
                    agent_to_codex_toml(frontmatter, rendered_body),
                    "agent",
                    [target],
                    compiled=True,
                )
            )
        else:
            requests.append(
                new_text_request(
                    f"{destination_root}/{path.name}",
                    source,
                    markdown_with_frontmatter(frontmatter, rendered_body),
                    "agent",
                    [target],
                )
            )
    return requests


def build_skill_requests(
    project_root: Path, skills_root: Path, skill_files: list[Path], target: str
) -> list[Request]:
    destination_root = TARGET_LAYOUTS[target].get("skills_destination")
    if not skill_files or destination_root is None:
        return []

    requests: list[Request] = []
    for path in skill_files:
        source = project_relative(path, project_root)
        relative = os.path.relpath(path, skills_root).replace("\\", "/")
        destination = f"{destination_root}/{relative}"
        if path.suffix.lower() in MARKDOWN_EXTENSIONS:
            requests.append(
                new_text_request(
                    destination,
                    source,
                    render_markdown(path.read_text(encoding="utf-8"), target),
                    "skill",
                    [target],
                )
            )
        else:
            requests.append(
                new_copy_request(destination, source, path, "skill", [target])
            )
    return requests


def enabled_targets_from_config(config: dict[str, Any], config_path: Path) -> list[str]:
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise ValueError(
            f"Config file '{config_path}' must contain a 'targets' object."
        )
    enabled: list[str] = []
    for name, entry in targets.items():
        normalized = str(name).lower()
        if normalized not in KNOWN_TARGETS:
            raise ValueError(f"Unknown target '{name}' in '{config_path}'.")
        if not isinstance(entry, dict) or "enabled" not in entry:
            raise ValueError(
                f"Target '{name}' in '{config_path}' must be an object with an 'enabled' boolean."
            )
        if bool(entry["enabled"]):
            enabled.append(normalized)
    return normalize_targets(enabled)


def active_targets(
    config: dict[str, Any], override: list[str] | None, config_path: Path
) -> list[str]:
    if override:
        selected = normalize_targets(override)
        unknown = [target for target in selected if target not in KNOWN_TARGETS]
        if unknown:
            raise ValueError(f"Unknown target(s): {', '.join(unknown)}.")
        return selected
    enabled = enabled_targets_from_config(config, config_path)
    if not enabled:
        raise ValueError(f"No targets are enabled in '{config_path}'.")
    return enabled


def assert_no_subset_collisions(
    active: list[str], enabled: list[str], explicit_override: bool, config_path: Path
) -> None:
    if not explicit_override:
        return
    unselected_enabled = [target for target in enabled if target not in active]
    if not unselected_enabled:
        return
    selected_paths = {path for target in active for path in target_destinations(target)}
    for target in unselected_enabled:
        for path in target_destinations(target):
            if path in selected_paths:
                joined = ", ".join(active)
                raise ValueError(
                    f"The target subset '{joined}' is unsafe because '{path}' is also shared with enabled target "
                    f"'{target}'. Run a full sync or disable '{target}' in '{config_path}'."
                )


def build_requests(
    project_root: Path, sync_root: Path, targets: list[str]
) -> tuple[list[Request], list[str]]:
    requests: list[Request] = []
    notices: list[str] = []

    main_doc = sync_root / "MAIN.md"
    commands_root = sync_root / "commands"
    agents_root = sync_root / "agents"
    skills_root = sync_root / "skills"
    command_files = list_matching_files(commands_root, "*.md")
    agent_files = list_matching_files(agents_root, "*.md")
    skill_files = list_skill_files(skills_root)

    for target in targets:
        doc_request, notice = build_doc_request(project_root, target, main_doc)
        if doc_request is not None:
            requests.append(doc_request)
        if notice is not None:
            notices.append(notice)

        command_requests, command_notices = build_command_requests(
            project_root, command_files, target
        )
        requests.extend(command_requests)
        notices.extend(command_notices)
        requests.extend(build_agent_requests(project_root, agent_files, target))
        requests.extend(
            build_skill_requests(project_root, skills_root, skill_files, target)
        )

    return merge_requests_by_destination(requests), notices


def write_request(
    project_root: Path,
    request: Request,
    what_if: bool,
    written: list[str],
    unchanged: list[str],
    compiled: list[str],
) -> None:
    destination_path = project_root / request.destination
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if request.mode == "text":
        existing = (
            destination_path.read_text(encoding="utf-8")
            if destination_path.exists()
            else None
        )
        if existing == request.content:
            unchanged.append(request.destination)
            return
        if not what_if:
            destination_path.write_text(request.content or "", encoding="utf-8")
        written.append(request.destination)
        if request.compiled:
            compiled.append(request.destination)
        return

    existing_hash = (
        sha256_bytes(destination_path.read_bytes())
        if destination_path.exists()
        else None
    )
    if existing_hash == request.hash_value:
        unchanged.append(request.destination)
        return
    if not what_if:
        if request.source_path is None:
            raise ValueError(
                f"Copy request for '{request.destination}' is missing a source path."
            )
        shutil.copy2(request.source_path, destination_path)
    written.append(request.destination)


def init_config() -> dict[str, Any]:
    return {
        "version": 1,
        "targets": {target: {"enabled": True} for target in KNOWN_TARGETS},
    }


def init_main_markdown() -> str:
    return """# Project Instructions

Write shared instructions for all targets here.

`codex`, `opencode`, and `cursor` all render to `AGENTS.md`, so their final root document must stay identical.

This line only appears in Claude output. [agentic-sync:claude]
"""


def initialize_project(project_root: Path) -> list[str]:
    sync_root = project_root / ".agentic-sync"
    if sync_root.exists():
        raise ValueError(f"Cannot initialize because '{sync_root}' already exists.")

    created: list[Path] = []
    sync_root.mkdir(parents=True)
    created.append(sync_root)

    for relative in ("commands", "agents", "skills"):
        path = sync_root / relative
        path.mkdir()
        created.append(path)

    config_path = sync_root / "config.json"
    write_json(config_path, init_config())
    created.append(config_path)

    main_path = sync_root / "MAIN.md"
    main_path.write_text(init_main_markdown(), encoding="utf-8")
    created.append(main_path)

    return [project_relative(path, project_root) for path in created]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--targets", nargs="+")
    parser.add_argument("--what-if", action="store_true")
    parser.add_argument("--project-root", default=str(Path.cwd()))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    sync_root = project_root / ".agentic-sync"
    config_path = sync_root / "config.json"

    if args.init:
        if args.targets:
            raise ValueError("--init cannot be combined with --targets.")
        if args.what_if:
            raise ValueError("--init cannot be combined with --what-if.")
        created = initialize_project(project_root)
        print(json.dumps({"initialized": True, "created": created}, indent=2))
        return 0

    if not config_path.exists():
        raise ValueError(f"Missing config file '{config_path}'.")

    config = read_json(config_path)
    enabled = enabled_targets_from_config(config, config_path)
    active = active_targets(config, args.targets, config_path)
    assert_no_subset_collisions(
        active, enabled, explicit_override=bool(args.targets), config_path=config_path
    )

    requests, notices = build_requests(project_root, sync_root, active)

    written: list[str] = []
    unchanged: list[str] = []
    compiled: list[str] = []

    for request in requests:
        write_request(project_root, request, args.what_if, written, unchanged, compiled)

    summary = {
        "targets": active,
        "written": written,
        "unchanged": unchanged,
        "compiled": compiled,
        "notices": notices,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
