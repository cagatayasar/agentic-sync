# agentic-sync

`agentic-sync.py` lets you maintain one canonical set of .md files for skills/agents/commands under `.agentic-sync/` and generate the Claude/Codex/Opencode/Cursor specific files each app expects, like `CLAUDE.md`, `AGENTS.md`, commands, agents, and skills.

## Basic Usage

`agentic-sync.py` takes one canonical source tree under `.agentic-sync/` and generates the right files for `claude`, `codex`, `opencode`, and `cursor`.

First-time setup:

```bash
python agentic-sync.py --init
```

That creates:

- `.agentic-sync/config.json` - enables targets
- `.agentic-sync/MAIN.md` - the shared root instruction doc
- `.agentic-sync/commands/` - custom command definitions
- `.agentic-sync/agents/` - agent definitions
- `.agentic-sync/skills/` - skill files, copied or rendered recursively

Sync usage:

```bash
python agentic-sync.py
```

Useful flags:

```bash
python agentic-sync.py --init
python agentic-sync.py --what-if
python agentic-sync.py --targets claude codex
```

What it generates:

- `claude` -> `CLAUDE.md`, `.claude/commands`, `.claude/agents`, `.claude/skills`
- `codex` -> `AGENTS.md`, `.codex/agents`, `.agents/skills`
- `opencode` -> `AGENTS.md`, `.opencode/commands`, `.opencode/agents`, `.opencode/skills`
- `cursor` -> `AGENTS.md`, `.cursor/commands`

How `MAIN.md` works:

- It is the single source for both root `CLAUDE.md` and root `AGENTS.md`.
- It supports inline and block directives like:

```md
Only for Claude. [agentic-sync:claude]

[agentic-sync-start:except=cursor]
Shown everywhere except Cursor.
[agentic-sync-end]
```

Important rule:

- `codex`, `opencode`, and `cursor` all share the same output file: `AGENTS.md`.
- So any target-specific directives in `MAIN.md` must still produce identical final content for those three, or the script will error.

Typical workflow:

1. Run `python agentic-sync.py --init` once.
2. Edit `.agentic-sync/MAIN.md` and any command/agent/skill source files.
3. Run `python agentic-sync.py --what-if` to preview.
4. Run `python agentic-sync.py` to write files.

Good to know:

- There is no fallback doc source path; the script only looks for `.agentic-sync/MAIN.md`.
- `--init` fails fast if `.agentic-sync/` already exists.
- The script only writes or updates current outputs; it does not track or delete stale generated files.
- The script prints a JSON summary of what it wrote, skipped, or compiled.

## Extras

- Agent files in `.agentic-sync/agents/*.md` need YAML frontmatter with at least `name` and `description`.
- Frontmatter can use the same inline and block directives as markdown body content; directives are rendered before YAML parsing.
- Codex agents are compiled to TOML instead of staying as markdown.
- Markdown sources are rendered through directives before writing; non-markdown skill files are copied as-is.
- If multiple targets would write different content to the same destination, the script fails instead of picking one.
- YAML frontmatter is parsed by the script's built-in parser.
