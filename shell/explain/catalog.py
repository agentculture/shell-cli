"""Markdown catalog for ``shell explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("shell",)`` and ``("shell-cli",)`` all resolve to the root entry — the
executable is ``shell``, the repo/distribution token is ``shell-cli``, and a
reader may reasonably reach for either.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# shell

The file-and-shell tool surface for AI coding agents: read, write, edit, list,
view media, and gated shell execution — with path confinement and an operator
approval policy. Packaged pure-stdlib so any harness imports one safe execution
layer instead of reimplementing it (and reimplementing its safety model).

Two surfaces, in priority order: a **library** (the primary consumer is another
harness importing it) and a **CLI** (drive the same gated surface from a
terminal and watch the policy decide).

## Status

The library has the operation core: `fs.read`, `fs.list`, `fs.write`, `fs.edit`
and `fs.media`, path confinement, the policy evaluator, the evidence contract,
and `HostRunner` execution are built and green.

**The CLI has not caught up** — only the introspection verbs below are
implemented here. Process execution and the `env` / `fs` / `process` / `git` /
`policy` / `operation` verb groups are not exposed yet. See
<https://github.com/agentculture/shell-cli/issues/1>.

## Safety posture

A guard, not a sandbox. The execution gate is best-effort: it refuses the
obvious and accidental case, and it is bypassable by `sh -c`, pipelines, and
shell expansion. It protects against careless model behaviour, not an
adversarial one. See `shell explain safety`.

## Verbs

- `shell whoami` — identity probe from `culture.yaml`.
- `shell learn` — structured self-teaching prompt.
- `shell explain <path>` — markdown docs for any noun/verb.
- `shell overview` — descriptive snapshot of the agent.
- `shell doctor` — check the agent-identity invariants.
- `shell cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `shell explain safety`
- `shell explain whoami`
"""

_SAFETY = """\
# shell safety model

**shell-cli ships a guard, not a sandbox.** This entry exists so the claim is
addressable in-band — an agent can read it before deciding what to trust.

## What it protects against

Accidental and careless behaviour: a path that escapes the repo root, a write
into a tree declared read-only, a command that would run something it should
not, output large enough to blow the caller's context window.

## What it does NOT protect against

An adversarial command. The execution gate inspects the raw command string, so
variable expansion, concatenation, here-docs, `sh -c`, and pipelines can all
defeat it. There is no namespace, container, or seccomp isolation on the
default path.

If you need real isolation, that is separate, deliberate work — it is not
implied by anything here.

## Layers (as extracted)

- **Path confinement** — relative paths resolve under a configured root;
  anything escaping it is refused.
- **Read-only subtrees** — configured paths may be read, never written.
- **Truncation** — every result is capped so one huge file or command cannot
  exhaust the caller's context.
- **Approval policy** — `run_command` is gated by an operator-supplied policy
  returning an explicit verdict; file operations can be checksum-pinned.

## Status

Not yet extracted from `colleague` — this documents the contract the extraction
must uphold, not shipped behaviour. See `docs/threat-model.md`.
"""

_WHOAMI = """\
# shell whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    shell whoami
    shell whoami --json
"""

_LEARN = """\
# shell learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    shell learn
    shell learn --json
"""

_EXPLAIN = """\
# shell explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    shell explain shell
    shell explain safety
    shell explain --json whoami
"""

_OVERVIEW = """\
# shell overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the artifacts this repo carries. Accepts an ignored `target`
so a stray path never hard-fails.

## Usage

    shell overview
    shell overview --json
"""

_DOCTOR = """\
# shell doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    shell doctor
    shell doctor --json
"""

_CLI = """\
# shell cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    shell cli overview
    shell cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("shell",): _ROOT,
    ("shell-cli",): _ROOT,
    ("safety",): _SAFETY,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
