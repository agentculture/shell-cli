"""Markdown catalog for ``shell-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("shell-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# shell-cli

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `shell-cli whoami` — identity probe from `culture.yaml`.
- `shell-cli learn` — structured self-teaching prompt.
- `shell-cli explain <path>` — markdown docs for any noun/verb.
- `shell-cli overview` — descriptive snapshot of the agent.
- `shell-cli doctor` — check the agent-identity invariants.
- `shell-cli cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `shell-cli explain whoami`
- `shell-cli explain doctor`
"""

_WHOAMI = """\
# shell-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    shell-cli whoami
    shell-cli whoami --json
"""

_LEARN = """\
# shell-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    shell-cli learn
    shell-cli learn --json
"""

_EXPLAIN = """\
# shell-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    shell-cli explain shell-cli
    shell-cli explain whoami
    shell-cli explain --json <path>
"""

_OVERVIEW = """\
# shell-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    shell-cli overview
    shell-cli overview --json
"""

_DOCTOR = """\
# shell-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    shell-cli doctor
    shell-cli doctor --json
"""

_CLI = """\
# shell-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    shell-cli cli overview
    shell-cli cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("shell-cli",): _ROOT,
    ("shell",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
