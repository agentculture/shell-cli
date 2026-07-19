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

**The CLI is catching up.** `policy` and `operation` are now real CLI nouns —
`policy check` / `policy explain` evaluate the same policy gate
`shell.operations.execute()` uses, and `operation show` retrieves persisted
evidence. The `env` / `fs` / `process` / `git` verb groups are still not
exposed — there is no CLI verb yet that itself *executes* `fs.read` or a
shell command, only ones that introspect the policy and evidence around that
execution. See <https://github.com/agentculture/shell-cli/issues/1>.

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
- `shell policy check <kind>` — evaluate the run_command policy gate for one
  operation, through the same evaluator `execute()` uses.
- `shell policy explain` — list gated kind prefixes and every known operation
  kind's policy status, ungated kinds included explicitly.
- `shell operation show <operation-id>` — retrieve a persisted evidence
  record (requires an evidence store to have been configured; see
  `docs/evidence-contract.md`).

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

_POLICY = """\
# shell policy

Evaluate and explain the operation-policy gate. Both verbs call
`shell.operations._policy_gate` directly — the exact function
`shell.operations.execute()` gates through — so a verdict from this noun is
never a re-derivation that could drift from what execution would actually do.

## Verbs

- `shell policy check <kind> [--command STR | --argv TOK ...] [--policy-file
  PATH]... [--policy-json JSON]` — evaluate the run_command gate for one
  `(kind, arguments)` pair. `<kind>` need not be a registered operation
  handler; jurisdiction is decided by the kind string
  (`GATED_KIND_PREFIXES = ("process.",)`), not by handler registration. A
  denied verdict is reported information, not a CLI failure — exit 0
  regardless of the decision; parse the `decision` field.
- `shell policy explain [--policy-file PATH]... [--policy-json JSON]` — lists
  the gated kind prefixes and, for every operation kind currently registered
  in this process, its explicit status. `fs.*` kinds are deliberately **not**
  policy-gated (confining file operations is the filesystem layer's job, not
  the run_command allow-list's) and are reported `ungated` explicitly rather
  than omitted — `ungated` and `allowed` are distinct
  `PolicyDecision` values, never conflated.
- `shell policy overview` — describe this noun.

## Usage

    shell policy check fs.read --json
    shell policy check process.shell --command "rm -rf /" --policy-file approvals.json
    shell policy explain --json

## See also

- `shell explain safety`
- `docs/evidence-contract.md`
"""

_OPERATION = """\
# shell operation

Retrieve persisted evidence for a previously executed operation. Reads only —
this noun never executes an operation itself.

## Verbs

- `shell operation show <operation-id> [--evidence-dir DIR]` — retrieve the
  persisted evidence record for `<operation-id>` from an
  `EvidenceStore` directory (default `./.shell/evidence`).
- `shell operation overview` — describe this noun.

## Persistence is opt-in

`shell.operations.execute()` only writes an evidence record when its caller
passes `evidence_store=...`; most invocations configure none. "No record
found" is therefore the ordinary case, not a bug — and this command reports
that honestly rather than implying an empty trail is the same thing as no
trail at all. Three cases are kept distinct: no evidence directory at the
given location (exit 1), a directory with records but none matching the
given id (exit 1, with the count of records actually present), and a
location that exists but cannot be read at all — not a directory, or a
permission/IO failure (exit 2, the one genuine environment-error path in this
verb surface).

## Usage

    shell operation show <operation-id>
    shell operation show <operation-id> --evidence-dir .shell/evidence --json

## See also

- `docs/evidence-contract.md`
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
    ("policy",): _POLICY,
    ("policy", "overview"): _POLICY,
    ("policy", "check"): _POLICY,
    ("policy", "explain"): _POLICY,
    ("operation",): _OPERATION,
    ("operation", "overview"): _OPERATION,
    ("operation", "show"): _OPERATION,
}
