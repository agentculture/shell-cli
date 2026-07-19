# shell-cli

**The guarded local operations plane for AI agents.**

An agent harness has to touch the local machine constantly — read a file, write
one, run a command, run the tests, run a linter, fire a hook, drive git, invoke
a trusted CLI. Every harness reimplements that surface, and reimplements its
safety model with it, usually worse each time.

shell-cli provides one place to **plan, authorize, execute, observe, and record**
a local operation. It does not decide *why*, *when*, or *which* — that is the
calling harness's job. It is not another agent: it contains no model, planner,
memory, web browser, or domain workflow. It is the execution substrate beneath
them.

[`colleague`](https://github.com/agentculture/colleague) is the first consumer,
not the owner. colleague imports shell-cli; shell-cli imports nothing from
colleague.

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

A container runner is planned, and when it lands it will be described
separately: a declared boundary with a documented profile, recorded per
operation. It will not retroactively change what the host path above provides.
The two claims stay textually separate on purpose.

Full detail — assets, actors, known bypasses, and what real isolation would
require — in [`docs/threat-model.md`](docs/threat-model.md). The same posture is
readable in-band via `shell explain safety`, and
[`tests/test_honesty.py`](tests/test_honesty.py) fails the build if any shipped
surface drops the disclaimer or claims isolation.

## Status

**Scaffold, with the release pipeline hardened.** The agent-first CLI skeleton
(`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli`) is real and green,
and publishing is gated on the full quality suite plus a built-wheel smoke test.

**No operation, environment, policy, or runner has been built yet.** The
introspection verbs below are the entire implemented surface today.

[Issue #1](https://github.com/agentculture/shell-cli/issues/1) is the source of
truth for scope; the converged spec and the buildable plan live in
[`docs/specs/`](docs/specs/) and [`docs/plans/`](docs/plans/).

## The model

An **operation** is any local observation, mutation, process invocation, or
environment lifecycle action. The boundary is **work-affecting I/O** — not every
internal byte a runtime writes. Every operation follows one lifecycle:

```text
intent -> Operation -> policy + preview -> environment backend
       -> result + evidence -> caller
```

### Environments have two independent axes

"Host", "worktree" and "container" are not one overloaded mode. A workspace axis
selects what may be observed and changed; a runner axis selects what executes
it.

| Workspace | Runner | Guarantee |
|---|---|---|
| Checkout | Host | Guarded host execution; no isolation |
| Worktree | Host | Reviewable/recoverable changes; no process isolation |
| Checkout | Container | Execution isolation against the selected checkout |
| Worktree | Container | Preferred autonomous mode |

### Three operation profiles

Every subprocess declares why it is running, because not all of them deserve
equal trust.

- **`project`** — executes repository-controlled code: model-issued commands,
  tests, linters, repo hooks.
- **`control`** — executes trusted control-plane programs: git mechanics,
  neighbouring AgentCulture CLIs, the container engine itself. Argv vectors
  only; raw shell strings are not appropriate here.
- **`observe`** — structured reads confined to the selected root.

### Evidence is a product surface

Every operation produces a structured record suitable for the model, the
operator, telemetry, and an external validator: what was requested and how it
normalized, the policy verdict, the environment and runner, timing and exit
status, separately captured stdout/stderr with truncation markers, and the known
filesystem/git effects — plus **an honest marker for whether that effect list is
complete**. Secrets are never recorded, and a failure to capture evidence never
turns an executed action into an unrecorded success.

## Constraints that hold it together

- **Pure-stdlib core, zero base dependencies.** `dependencies = []`, guarded by a
  test. colleague allow-lists exactly its sanctioned base deps; a third-party
  dependency here means colleague cannot import this package at all. Optional
  integrations go behind extras.
- **A guard, not a sandbox** — see above.
- **Mutation and execution preview by default.** The CLI requires `--apply`;
  imported callers must pass `apply=True` explicitly. A preview is never
  reported as success — it gets its own status.
- **Library first, CLI second.** The CLI is a front end over the same operation
  engine, never a second implementation.
- **Every PR bumps the version** — every push to `main` publishes to PyPI. See
  [`docs/release-runbook.md`](docs/release-runbook.md).

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

## Non-goals

shell-cli is deliberately not a general agent layer. It owns local operation
mechanics and nothing else:

- reasoning, planning, roles, or agent orchestration — colleague's job
- model APIs or prompt construction
- **web search and navigation semantics** — `webglass-cli` owns them, as a
  separate repository and a separate colleague capability seam. shell-cli does
  not embed a browser and does not model web actions as local operations. Only
  provider-neutral artifact transfer crosses between the two: a file webglass
  produces is just a file shell-cli can be asked to read.
- memory semantics — `eidetic-cli` owns them
- PR policy, acceptance judgment, or handoff decisions
- claiming containers are perfect isolation, or that worktrees contain anything

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
