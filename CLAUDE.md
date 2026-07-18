# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**shell-cli owns the file-and-shell tool surface for AI coding agents.** An agent
harness needs a small, boring, *safe* set of primitives: read a file, write a
file, edit a file, list a directory, look at an image, and run a shell command.
Today that surface lives inside
[`colleague`](https://github.com/agentculture/colleague) welded to one host, so
every other harness reimplements it — and reimplements the safety model with it.

This repo lifts that surface out into a reusable, pure-stdlib package. colleague
becomes the first consumer, not the owner. The authoritative build brief is
[issue #1](https://github.com/agentculture/shell-cli/issues/1) — read it before
large-scale work; it is the source of truth for scope.

Identity is settled and must not drift: repo token `shell-cli`, console command
**`shell`**, import package **`shell`**, PyPI distribution **`shell-cli`**.

### Current state

The repo is **scaffold only**. The agent-first CLI skeleton (`whoami`, `learn`,
`explain`, `overview`, `doctor`, `cli`) is real and green; **none of the six
primitives, the path confinement, or the approval policy has been extracted
yet.** Much of the CLI's self-description still calls this "a clonable template
for AgentCulture mesh agents" — that is leftover scaffold prose, not the mission.
Fix it as you touch it (see *Known scaffold drift* below).

## Commands

```bash
uv sync                                  # create/refresh .venv
uv run pytest -n auto                    # full suite (xdist)
uv run pytest tests/test_cli.py -v       # one file
uv run pytest -k test_whoami_json -v     # one test
uv run pytest --cov=shell --cov-report=term   # with coverage (fail_under=60)

uv run shell whoami                      # the console command is `shell`, NOT `shell-cli`
uv run shell doctor --json

# The four lint gates CI runs, in CI order:
uv run black --check shell tests
uv run isort --check-only shell tests
uv run flake8 shell tests
uv run bandit -c pyproject.toml -r shell
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills"
uv run teken cli doctor . --strict       # the agent-first rubric gate
```

`teken` is a **dev dependency only** — it renders/validates the CLI shape; it
must never become a runtime dependency.

## Non-negotiable constraints

These four are contracts, not preferences. Violating any of them breaks a
downstream consumer or an explicit honesty commitment.

### 1. The core is pure-stdlib — zero base dependencies

colleague pins exactly one base dependency (`agentfront>=0.20.0`) and guards it
with `tests/test_zero_deps.py`, which allow-lists that single name. The operator
decided shell-cli clears the same bar and gets allow-listed as a **second**
sanctioned base dep. That means the core may import **nothing outside the Python
standard library, ever** — if you take a third-party dep, colleague cannot import
you at all.

`pyproject.toml` already has `dependencies = []`. Keep it that way. Anything
optional (a richer renderer, an MCP surface, container backends) goes in a
`[project.optional-dependencies]` extra, following colleague's `[mcp]` / `[otel]`
/ `[tui]` pattern.

**Write the guard test early** — mirror colleague's
`/home/spark/git/colleague/tests/test_zero_deps.py`, which does two things worth
copying: asserts the declared `[project].dependencies` list literally, and
snapshots `sys.modules` before/after importing the package to catch third-party
leaks that a manifest check would miss. Enforce it in CI rather than remembering
it.

### 2. It is a guard, not a sandbox — never upgrade the claim

A package *named* `shell-cli` whose headline is safe execution will be read as
offering a sandbox. It does not. colleague's own comments are candid about this
and that posture must survive the extraction verbatim:

> Honest limitation: the guard is a substring check on the raw command string. A
> sufficiently obfuscated command (e.g. variable expansion, concatenation,
> here-docs) could bypass it. It is best-effort — an airtight sandbox is out of
> v0 scope. The guard covers the obvious / accidental case; document rather than
> overclaim.
>
> Honest limit: like the rest of this gate, it is bypassable by `sh -c`,
> pipelines, and shell expansion — **a guard, not a sandbox.**

State the threat model explicitly and prominently in the README: this protects
against **accidental and careless** model behaviour, not against an adversarial
one. Do not let a docstring, a README line, or a commit message quietly imply
isolation the code does not provide.

### 3. Write verbs are dry-run by default; `--apply` commits

Agents call CLIs in loops. Any verb that mutates the filesystem previews by
default and only writes when `--apply` is passed. Safe-by-default is mandatory,
not a nicety.

### 4. Every PR bumps the version

Every push to `main` publishes to PyPI, and the `version-check` CI job fails a PR
whose `pyproject.toml` version matches `main` — including docs-only and CI-only
PRs. Use the `version-bump` skill (`/version-bump patch|minor|major`); it updates
`pyproject.toml` and prepends a Keep-a-Changelog entry.

## Architecture

### The two surfaces, in priority order

You are building two things, and the order matters:

1. **A library** — the primary consumer is colleague *importing* you. This is the
   surface that carries the value. Design it first.
2. **A CLI** (`shell read`, `shell run`, `shell edit`) — so a human or an agent
   can drive the same gated surface from a terminal and watch the policy decide.

Do not let CLI ergonomics distort the library API.

### CLI scaffold conventions (already established — follow them)

The afi-cli/teken pattern, no `src/` wrapper, one module per subcommand under
`shell/cli/_commands/`. Each module exposes a `register(sub)` that adds its
parser and sets `func`; `shell/cli/__init__.py:_build_parser` wires them up.
Noun groups nest their own subparsers the same way (see `_commands/cli.py`).

Three contracts are load-bearing and enforced by tests plus the `teken cli
doctor --strict` rubric gate:

- **Errors never leak tracebacks.** Every failure raises `CliError`
  (`shell/cli/_errors.py`); `_dispatch` catches it, routes it through
  `emit_error`, and wraps any unexpected exception. Argparse errors are folded
  into the same shape by `_CliArgumentParser.error`, with a `_json_hint`
  class attribute pre-scanned from raw argv so parse-time failures still honour
  `--json`. New subparsers must be built with `parser_class=_CliArgumentParser`
  or they bypass this and exit 2 with raw argparse output.
- **Streams never mix.** Results to stdout, errors and diagnostics to stderr
  (`shell/cli/_output.py`). Text-mode errors render `error: …` then `hint: …` —
  the `hint:` prefix is what agent consumers parse.
- **Exit codes**: `0` success, `1` user error, `2` environment error, `3+`
  reserved.

Every verb takes `--json`. Every registered noun/verb path needs an entry in
`shell/explain/catalog.py`; `tests/test_cli.py::test_every_catalog_path_resolves`
walks the whole catalog, and the rubric gate requires any noun with action-verbs
to expose `overview`.

`whoami`/`doctor` parse `culture.yaml` with a hand-rolled line scanner
(`_commands/whoami.py`) specifically to avoid a YAML dependency — re-read the
pure-stdlib constraint before "improving" that.

### What to extract from colleague, and from where

Surveyed against the live checkout at `/home/spark/git/colleague` (colleague
1.51.0). `colleague/tools.py` is 1360 lines declaring its tool schemas as a flat
list; six are yours, the rest are colleague's own.

| Yours (extract) | colleague-specific (leave behind) |
|---|---|
| `read_file` | `culture` |
| `view_media` | `devague` |
| `write_file` | `subagent` / `subagents` |
| `edit_file` | `memory` |
| `list_dir` | `run_tests` / `check_test_integrity` |
| `run_command` | `finish`, `deepthink` |

Handlers live in `ToolExecutor` (`colleague/tools.py:666`): `_read_file:813`,
`_view_media:827`, `_write_file:860`, `_edit_file:878`, `_list_dir:947`,
`_run_command:957`.

**The safety machinery moves with them — this was decided explicitly and it is
the point of the split.** A package shipping the six primitives *without* the
safety model would be a thin wrapper worth very little.

- `_safe_path` (`tools.py:730`) — resolves a relative path under the root and
  refuses anything that escapes it.
- `_refuse_clone_write` (`tools.py:737`) — refuses writes into a read-only source
  subtree. **Generalise this**: a configurable set of read-only paths, not a
  hard-coded `.colleague/neighbours`.
- `_truncate` (`tools.py:724`) — caps each result so a huge file or command
  output cannot blow the model's context window.
- `_number_lines` (`tools.py:599`) and the `_require` argument-validation helpers.
- The `run_command` approval policy — `colleague/policy.py` (400 lines): the
  frozen `Verdict` dataclass (`:80`), `Policy.check_run_command` (`:205`),
  `Policy.check_file` (`:249`), the checksum helpers `file_checksum` (`:101`) /
  `verify_checksum` (`:121`), `_first_token` (`:309`), and policy-file loading
  (`_parse_policy_file:326`, `load_policy:354`). colleague gates only
  `run_command` today (`colleague/loop.py:895-903`); everything else passes
  through.

### The real work is decoupling, not copying

Both files are stdlib-clean but **not** import-clean. Two couplings must break:

- `tools.py` does `from colleague import culture, devague, media, memory,
  testintegrity` and `from colleague.config import _DEFAULT_MAX_OUTPUT_CHARS,
  MAX_SUBAGENT_FANOUT`. Most of that belongs to tools you are *not* taking — but
  **`media` is imported for `view_media`, which is yours.** Decide deliberately:
  either `media` travels along, or `view_media` stays behind. Say which and why.
- `policy.py` does `from colleague.configdir import resolve_file` and `from
  colleague.layers import sanitize_model`. Both must become injected parameters
  or travel along.

`ToolExecutor` is one class mixing both halves, so extraction means splitting it.
The encouraging part: its constructor is already injection-shaped — `spawn`,
`batch_spawn`, `deepthink` callables plus an `allowlist`
(`tools.py:669-723`). A natural seam is shell-cli owning the base executor (six
primitives + confinement + policy) with colleague subclassing it and registering
its own tools on top. **Propose the seam before building it; do not assume that
shape is the only one.**

Tests worth mining from `/home/spark/git/colleague/tests/`:
`test_loop_run_command_policy.py`, `test_policy_carveout.py`,
`test_policy_all_engines.py`, `test_write_apply_isolation.py`,
`test_zero_deps.py`.

### Planned: VM / in-container execution

Beyond issue #1, the intended direction is a **real** isolation backend —
running `run_command` inside a container or VM rather than the host shell. Two
things to hold onto when that work starts:

- It does not retroactively make the *existing* guard a sandbox. Keep the two
  claims separate: the substring/token guard is best-effort host-side; a
  container backend is genuine isolation. Never let one sentence cover both.
- Constraint #1 still binds. Driving `docker`/`podman` by `subprocess` to a CLI
  on `PATH` keeps the core pure-stdlib; pulling a Docker SDK does not, and would
  have to live behind an extra.

## Open questions — park them, do not guess

Recorded in issue #1; still unanswered. Use `/think` (devague) if one needs real
framing rather than a quick call.

- Does `media` / `view_media` travel here, or stay in colleague?
- Does the approval-policy *file format* stay colleague's, or does shell-cli
  define its own and colleague adapt? (Policy files exist in the wild; a format
  change is a migration.)
- Is the read-only-subtree guard generalised to arbitrary paths, or does it stay
  a single configurable directory?
- Does the CLI expose the policy as a first-class verb (`shell policy check …`)?
- Does anything beyond colleague adopt this soon — and should that shape the API
  now, or wait for a second real consumer?

## Definition of done, first milestone

1. Six primitives + confinement + truncation + approval policy live here,
   pure-stdlib, with a `test_zero_deps`-style guard enforcing it.
2. Tests carried over and extended from the colleague suite named above.
3. A documented threat model that does **not** claim to be a sandbox.
4. A concrete migration proposal **filed as an issue on colleague**: how it drops
   its copy, allow-lists `shell-cli` in its guard test, and keeps behaviour
   identical. colleague's change is colleague's to make — **propose, don't push.**

## Known scaffold drift

Real inconsistencies in the tree right now. Fix them as you touch the
surrounding code; don't treat them as intended behaviour.

- **`shell` vs `shell-cli`.** The console script is `shell`
  (`[project.scripts]`), but argparse's `prog` is `"shell-cli"`, so `--help`,
  every `explain` catalog body, and the `learn` text all print commands
  (`shell-cli whoami`) that do not exist as an executable. The README's
  quickstart (`uv run shell-cli whoami`) fails outright.
- **Scaffold self-description.** `learn`, `explain`, and `overview` describe this
  repo as "a clonable template for AgentCulture mesh agents" and list
  template-onboarding artifacts. That is the template it was cloned from, not
  what this agent is.
- **Prompt-file story.** `culture.yaml` declares `backend: colleague`, so
  `doctor`'s backend-consistency check requires `AGENTS.colleague.md` — which
  exists and passes. This `CLAUDE.md` is the Claude Code prompt and does **not**
  change that mapping; keep both files, and keep them from contradicting each
  other.

## Conventions and workflow

- **Skills**: `.claude/skills/` is vendored cite-don't-import from guildmaster
  (and directly from devague/colleague for a few tracked divergences). Provenance
  and the re-sync procedure live in `docs/skill-sources.md` — read it before
  editing anything under `.claude/skills/`; local edits are lost on re-sync
  unless lifted upstream first. Vendored skills are excluded from markdownlint
  and from Sonar analysis on purpose.
- **PR lane**: use the `cicd` skill (`devex pr` plus SonarCloud `status` /
  `await`). CI blocks on the Sonar quality gate when `SONAR_TOKEN` is set.
- **Second opinion**: reach for `ask-colleague` reflexively — `review` before
  opening a PR on a non-trivial diff, `explore` for a fresh read of an
  unfamiliar area. Both are read-only and run in a throwaway worktree, so the
  reflex is always safe; the side-effecting `write --apply` / `--pr` needs the
  user's go-ahead. Given that colleague is *the* consumer of this package, its
  read of an extraction seam is worth more here than usual.
- **Memory**: `/recall` before non-trivial work to build on prior decisions
  rather than re-deriving them; `/remember` when a non-obvious decision,
  constraint, fix-and-why, or hard-won gotcha surfaces. This repo's memory is
  in-repo and public — records resolve to `<repo-root>/.eidetic/memory`
  (committed, team- and mesh-shared). Pass `--visibility private` to route a
  record to `$HOME` instead.
- **Cross-repo**: use the `communicate` skill to file issues on sibling repos
  (auto-signs `- shell-cli (Claude)`); use `gh issue create` or `cicd` for issues
  on this repo.
