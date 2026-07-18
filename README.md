# shell-cli

The file-and-shell tool surface for AI coding agents: read, write, edit, list,
view media, and gated shell execution — with path confinement and an operator
approval policy. Pure-stdlib core, extracted from
[`colleague`](https://github.com/agentculture/colleague) so any harness imports
one execution layer instead of reimplementing it, and reimplementing its safety
model with it.

Repo token `shell-cli`, console command **`shell`**, import package **`shell`**,
PyPI distribution **`shell-cli`**.

## ⚠️ A guard, not a sandbox

**Read this before you rely on anything here.**

The execution gate is **best-effort**. It inspects the command string it is
handed, so it is bypassable by `sh -c`, pipelines, shell expansion, here-docs,
and any interpreter that takes code as an argument. There is no namespace,
container, or seccomp isolation.

It protects against **accidental and careless** model behaviour. It does **not**
protect against an adversarial one, and it is not a security boundary to put
untrusted input behind.

Full detail — assets, actors, known bypasses, and what real isolation would
require — in [`docs/threat-model.md`](docs/threat-model.md). The same posture is
readable in-band via `shell explain safety`, and
[`tests/test_honesty.py`](tests/test_honesty.py) fails the build if any shipped
surface drops the disclaimer or claims isolation.

## Status

**Scaffold.** The agent-first CLI skeleton is real and green; the six
primitives, the path confinement, and the approval policy are still being
extracted from `colleague`. Only the introspection verbs below are implemented
today. The build brief and current scope live in
[issue #1](https://github.com/agentculture/shell-cli/issues/1).

## What this will be

Two surfaces, in priority order:

1. **A library** — the primary consumer is another agent harness *importing*
   this. That is the surface carrying the value, and it is designed first.
2. **A CLI** — `shell read`, `shell run`, `shell edit`, so a human or an agent
   can drive the same gated surface from a terminal and watch the policy decide.

Four constraints hold it together:

- **Pure-stdlib core, zero base dependencies.** colleague allow-lists exactly
  its sanctioned base deps; a third-party dependency here means colleague cannot
  import this package at all. Optional features go behind extras.
- **A guard, not a sandbox** — see above.
- **Write verbs are dry-run by default**; `--apply` commits. Agents call CLIs in
  loops, so safe-by-default is mandatory.
- **Every PR bumps the version** — every push to `main` publishes to PyPI.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run shell whoami                   # identity from culture.yaml
uv run shell learn                    # self-teaching prompt (add --json)
uv run shell explain safety           # the safety posture, in-band
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path (`explain safety` for the threat model). |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## Repo furniture

- **Mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`). `CLAUDE.md` is the Claude Code prompt and the
  contributor guide.
- **The canonical guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. Provenance and the re-sync procedure:
  [`docs/skill-sources.md`](docs/skill-sources.md).
- **Build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
