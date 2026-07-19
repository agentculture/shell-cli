# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**shell-cli is the guarded local operations plane for AI agents.**

An agent harness has to touch the local machine constantly — read a file, write
one, run a command, run the tests, run a linter, fire a hook, drive git, invoke
a trusted CLI. Every harness reimplements that surface, and reimplements its
safety model with it, usually worse each time.

shell-cli provides one place to **plan, authorize, execute, observe, and record**
a local operation. It does not decide *why*, *when*, or *which* — that is the
calling harness's job.

```text
Colleague                 shell-cli                    capability CLIs
-----------------------   --------------------------   ----------------------
reason and plan           materialize environment      devague semantics
choose capabilities       normalize operation          eidetic semantics
apply roles               evaluate operation policy    culture semantics
orchestrate agents        execute locally              coherence semantics
interpret results         capture effects/evidence     webglass web semantics
validate completion       report structured outcome
```

shell-cli is not another agent. It contains no model, planner, memory, web
browser, or domain workflow. It is the execution substrate beneath them.

[`colleague`](https://github.com/agentculture/colleague) is the first consumer,
not the owner.

**[Issue #1](https://github.com/agentculture/shell-cli/issues/1) is the source of
truth for scope.** Read it before large-scale work. Where this file and the issue
disagree, the issue wins and this file is the bug. Note that issue #1
*supersedes* an earlier, narrower "extract six tools from colleague" framing —
that extraction survives only as the first compatibility slice, not as the
mission.

Identity is settled and must not drift: repo token `shell-cli`, console command
**`shell`**, import package **`shell`**, PyPI distribution **`shell-cli`**.

### Current state

The repo is **scaffold only**. The agent-first CLI skeleton (`whoami`, `learn`,
`explain`, `overview`, `doctor`, `cli`, `explain safety`) is real and green;
**no operation, environment, policy, or runner has been built yet.** The CLI says
so itself — `learn`, `overview`, and the `explain` root each carry a Status
section. Keep those honest as the work lands; they are the first thing an agent
consumer reads.

## Commands

```bash
uv sync                                  # create/refresh .venv
uv run pytest -n auto                    # full suite (xdist)
uv run pytest tests/test_cli.py -v       # one file
uv run pytest -k test_whoami_json -v     # one test
uv run pytest --cov=shell --cov-report=term   # with coverage (fail_under=60)

uv run shell whoami                      # the console command is `shell`, NOT `shell-cli`
uv run shell doctor --json

# The lint gates CI runs, in CI order:
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

These are contracts, not preferences. Violating any of them breaks a downstream
consumer or an explicit honesty commitment.

### 1. The core is pure-stdlib — zero base dependencies

colleague pins exactly one base dependency (`agentfront>=0.20.0`) and guards it
with `tests/test_zero_deps.py`, which allow-lists that single name. The operator
decided shell-cli clears the same bar and gets allow-listed as a **second**
sanctioned base dep. The core may import **nothing outside the Python standard
library, ever** — if you take a third-party dep, colleague cannot import you at
all.

`pyproject.toml` has `dependencies = []`. Keep it that way.

- Git, Docker, and Podman are **external executables discovered on PATH**, driven
  by `subprocess`. A Docker SDK is a dependency and is therefore not an option
  for the core.
- Optional protocol/rendering integrations go in a
  `[project.optional-dependencies]` extra, following colleague's `[mcp]` /
  `[otel]` / `[tui]` pattern.
- **The zero-dependency/import-leak guard ships in the first implementation PR**,
  not later. Mirror the colleague checkout's `tests/test_zero_deps.py`, which is
  worth reading in full. Its core helper snapshots `sys.modules` before/after an
  action, reduces new entries to top-level names, and filters stdlib
  (`sys.stdlib_module_names`), the own package, import-system builtins, and
  exactly one sanctioned third party. Two patterns there transfer directly: run
  the check in a **fresh subprocess** when test-order independence matters, and
  **scan source text** when environment independence matters.

### 2. It is a guard, not a sandbox — never upgrade the claim

Structured filesystem operations are path-confined. A raw host shell command is
not: it can leave the repository through shell expansion, interpreters, absolute
paths, network calls, or child processes. The gate inspects a string that a shell
will later re-interpret, so `sh -c`, pipelines, here-docs, and variable expansion
all defeat it.

This protects against **accidental and careless** model behaviour, not an
adversarial one. Documentation *and result metadata* must say so plainly.

`tests/test_honesty.py` enforces this mechanically across `learn`, the `explain`
root, `explain safety`, `README.md`, `CLAUDE.md`, and `docs/threat-model.md`. It
bans affirmative isolation claims and permits the word only in a negating
sentence. If you find that test inconvenient, change the wording, not the test.

**When the container runner lands, the two claims stay textually separate.** The
host guard remains best-effort; a container is a *declared isolation boundary
with a documented profile*. One sentence must never cover both, and the container
work does not retroactively upgrade the host path.

### 3. Mutation and execution preview by default

Agents call CLIs in loops.

- **CLI**: mutation and execution verbs preview by default and require `--apply`.
  Reads execute immediately.
- **Library**: imported callers must state `apply=True` explicitly. There is no
  implicit apply.
- **A preview is never reported as success.** It gets its own status. A shell
  preview describes what *would* run; it does not pretend to predict effects.

The colleague compatibility adapter passes `apply=True` for today's
immediately-applied `write_file` / `edit_file` / `run_command` semantics.

### 4. Every PR bumps the version

Every push to `main` publishes to PyPI, and the `version-check` CI job fails a PR
whose `pyproject.toml` version matches `main` — including docs-only and CI-only
PRs. Use the `version-bump` skill (`/version-bump patch|minor|major`); it updates
`pyproject.toml` and prepends a Keep-a-Changelog entry.

## Architecture

### The operation is the core abstraction

An **operation** is any local observation, mutation, process invocation, or
environment lifecycle action. The boundary is **work-affecting I/O** — not every
internal byte a runtime writes. Runtime-private bookkeeping (artifacts, trace
feeds, telemetry buffers, caches, lock files) stays with its owning runtime
unless it executes code or touches the target workspace. That limit is what stops
shell-cli becoming a god-layer around ordinary application internals.

Every operation follows one lifecycle:

```text
intent -> Operation -> policy + preview -> environment backend
       -> result + evidence -> caller
```

The library contract is provider-neutral and JSON-serializable:

```python
operation = Operation(
    kind="process.exec",
    arguments={"argv": ["python", "-m", "pytest"]},
    profile="project",
    apply=True,
    caller={"agent": "colleague", "task_id": "...", "tool": "run_tests"},
)

result = operations.execute(operation, environment)
```

Names may improve during design; the semantics are fixed.

- **`Operation`** — stable id; kind and normalized arguments; intent (`observe`,
  `mutate`, `execute`, `lifecycle`); execution profile; caller/provenance;
  explicit preview/apply intent; timeout and resource request.
- **`OperationResult`** — status (`previewed`, `denied`, `succeeded`, `failed`,
  `timed_out`); structured output plus a bounded rendering; policy verdict and
  reason; known effects (changed paths, bytes written, git refs/diff, created
  resources); evidence (backend, root, cwd, mounts, network, timing, exit code,
  truncation); an **honest completeness marker** for effects that cannot be fully
  observed; neutral media observation when applicable.

### Environments have two independent axes

Do not encode "host", "worktree", and "Docker" as one overloaded mode.

- **Workspace/root axis** — operator checkout, caller-provided worktree, or a
  shell-managed worktree once that capability migrates.
- **Runner axis** — `HostRunner`, `ContainerRunner` (Docker/Podman), and a future
  VM/remote runner behind the same contract.

| Workspace | Runner | Guarantee |
|---|---|---|
| Checkout | Host | Guarded host execution; no isolation |
| Worktree | Host | Reviewable/recoverable changes; no process isolation |
| Checkout | Container | Execution isolation against the selected checkout |
| Worktree | Container | Preferred autonomous mode: isolated execution + reviewable changes |

colleague already uses **worktree + host** for `work`/`drive` and parallel
children; interactive sessions run **checkout + host**; failed worktree creation
may degrade to in-place execution. Preserve those behaviours during migration
rather than rebuilding a simpler competing worktree system.

An environment distinguishes at least: `source_root` (trusted control context),
`work_root` (what operations may observe/change), `runner`, read-only paths,
mount policy, network policy, environment-variable and secret policy, user
identity, resource limits, and timeout defaults.

**Policy is snapshotted from trusted control context before model mutations.** An
agent must not be able to edit its own active authorization by changing a file
inside the work root.

### Three operation profiles

Every subprocess declares why it is running. Not all `subprocess.run` calls
deserve equal trust.

- **`project`** — executes repository-controlled code: model-issued shell
  commands, tests, lint/format tools, affected-test runners, repo hooks
  configured as project code. Target: containerized by default, network disabled
  unless explicitly granted.
- **`control`** — executes trusted agent/control-plane programs: git
  worktree/handoff mechanics, the `devague` / `eidetic` / `culture` / `coherence`
  CLIs, neighbour clone management, Docker/Podman itself. Host execution is
  expected, but with argv vectors, executable allow-lists, minimal environment
  inheritance, explicit cwd, bounded output, and evidence. **Raw shell strings are
  not appropriate for control operations.**
- **`observe`** — structured reads: file listing, text/media loading, status,
  diff. Confined to the selected root; never implies process isolation.

### Layered safety model

Four distinct layers. Do not merge their claims.

1. **Capability authorization — colleague.** Roles decide which semantic tools may
   be offered and invoked.
2. **Operation policy — shell-cli.** The normalized operation is allowed, denied,
   or previewed under an operator-supplied policy snapshot.
3. **Execution isolation — runner.** Host mode is a guard; container mode is a
   declared isolation boundary with a documented profile.
4. **Outcome validation — colleague.** Tests, lint, integrity, acceptance, and
   handoff gates decide whether the work is acceptable.

### Evidence is a product surface

Every operation produces a structured evidence record suitable for the model, the
operator, telemetry, and an external validator. Minimum: operation id, caller,
task and tool; requested and normalized operation; preview/applied state; policy
verdict and matched rule; environment id, workspace kind, runner, root and cwd;
mounts, network and resource profile; start/end/duration; exit code or structured
error; **separately captured stdout/stderr** with bounded rendering; truncation
markers plus hashes/byte counts of full output when known; known filesystem/git
effects and **whether that effect list is complete**.

Secrets are never recorded. Evidence failure must not turn an executed action
into an unrecorded success — the result marks degraded evidence honestly.

colleague maps neutral results into its own `ToolOutcome`, `Step`, media message,
progress, statistics, and artifact shapes. **shell-cli must not import colleague
contracts.**

### Library first, CLI second

The importable API is primary. The CLI is a front end over exactly the same
operation engine — never a second implementation.

```text
shell env describe|create|destroy
shell fs read|list|stat|write|edit|media
shell process exec|shell
shell git status|diff|worktree|commit|merge
shell policy check|explain
shell operation show
shell whoami|learn|explain|doctor
```

Compatibility aliases (`shell read`, `shell edit`, `shell run`) may exist, but
must not distort the library model. Every command supports structured JSON;
results to stdout, diagnostics to stderr. Long-running operations support
streaming events and cancellation.

### Target package shape

Illustrative, not a mandate on filenames:

```text
shell/
├── operations.py       # Operation and dispatch
├── results.py          # neutral results/effects
├── environment.py      # source/work roots + profiles
├── policy.py           # evaluator and versioned policy data
├── evidence.py         # events, redaction, rendering
├── fs/                 # confined file/media operations
├── process/            # argv and raw-shell operations
├── git/                # generic git/worktree primitives
├── runners/
│   ├── host.py
│   └── container.py
└── cli/
```

**Avoid a giant `Shell` god object.** Operation handlers stay small and
composable behind one lifecycle pipeline. Do not add abstractions with no current
colleague or CLI consumer.

### CLI scaffold conventions (already established — follow them)

The afi-cli/teken pattern, no `src/` wrapper, one module per subcommand under
`shell/cli/_commands/`. Each module exposes a `register(sub)` that adds its parser
and sets `func`; `shell/cli/__init__.py:_build_parser` wires them up. Noun groups
nest their own subparsers the same way (see `_commands/cli.py`).

Three contracts are load-bearing and enforced by tests plus the `teken cli doctor
--strict` rubric gate:

- **Errors never leak tracebacks.** Every failure raises `CliError`
  (`shell/cli/_errors.py`); `_dispatch` catches it, routes it through
  `emit_error`, and wraps any unexpected exception. Argparse errors are folded
  into the same shape by `_CliArgumentParser.error`, with a `_json_hint` class
  attribute pre-scanned from raw argv so parse-time failures still honour
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
to expose `overview`. The canonical CLI groups above roughly triple the current
catalog, so **doc authoring is per-slice work, not a final pass**.

`whoami`/`doctor` parse `culture.yaml` with a hand-rolled line scanner
(`_commands/whoami.py`) specifically to avoid a YAML dependency — re-read the
pure-stdlib constraint before "improving" that.

## Where every colleague operation goes

The *mechanism* moves; domain intent stays with its owner.

| Current colleague action | Target operation | Semantic owner |
|---|---|---|
| `read_file`, `list_dir` | `fs.read`, `fs.list` | shell-cli |
| `write_file`, `edit_file` | `fs.write`, `fs.edit` | shell-cli |
| `view_media` | neutral confined `fs.media` | shell-cli; colleague adapts payload |
| `run_command` | `process.shell`, profile `project` | shell-cli |
| `run_tests` | curated `process.exec`, profile `project` | colleague picks argv |
| lint / affected tests | `process.exec`, profile `project` | colleague picks gate |
| hooks | `process.exec` with declared profile | colleague owns lifecycle |
| worktree add/remove/prune | generic git/worktree operations | colleague owns branch lifecycle |
| commit/diff/merge/handoff | generic git operations | colleague owns handoff semantics |
| neighbour cloning | git control operation | colleague selects neighbours |
| devague / culture / eidetic / coherence | trusted CLI control operation | each CLI owns semantics |
| subagent launch | child environment request | colleague owns orchestration |
| `finish`, `deepthink` | not a local shell operation | colleague |
| web search/navigation | not shell semantics | `webglass-cli` |
| artifacts, flight feeds, telemetry, caches, locks | runtime-private bookkeeping | owning runtime |

The end state makes direct process creation in colleague exceptional and
mechanically guarded. Project-code execution must not bypass the selected
environment merely because it arrived via `run_tests`, lint, or a hook instead of
`run_command`.

## What the survey found (ground truth, colleague 1.51.0)

Surveyed against a live colleague checkout at the pinned SHA below. These facts
should save you a re-derivation.

### The six primitives are cleaner than expected

Handlers live in `ToolExecutor` (`colleague/tools.py:666`): `_read_file:813`,
`_view_media:827`, `_write_file:860`, `_edit_file:878`, `_list_dir:947`,
`_run_command:957`.

- **Five of the six are import-clean** — they reference zero colleague modules and
  zero colleague config constants. `view_media` is the only coupled handler, and
  its only impurity is `colleague.media`.
- `_DEFAULT_MAX_OUTPUT_CHARS` is merely the constructor default
  (`tools.py:675`); handlers read `self._max_output_chars`. It inlines as `25000`
  with no behaviour change. `MAX_SUBAGENT_FANOUT` and `SubResult` are untouched by
  the six.
- The six touch only `root`, `changed`, `bytes_written`, `_max_output_chars`, and
  the class-level `_CLONE_SUBDIR`. The injected `spawn` / `batch_spawn` /
  `deepthink` callables and `_allowlist` are irrelevant to them. **This is why
  composition works and subclassing is unnecessary.**
- `colleague/media.py` is 111 lines and fully stdlib-clean, but it **cannot move**
  — `flatten_parts` and `IMAGE_TOKEN_ESTIMATE` serve five colleague-only call
  sites. `view_media` needs only `_MEDIA_TYPES` + `validate_attachment` +
  `build_part` (~50 lines). Vendor that slice; colleague keeps its copy.

Safety helpers to carry: `_safe_path` (`tools.py:730`), `_refuse_clone_write`
(`tools.py:737` — **generalise to a configurable set of read-only paths**),
`_truncate` (`tools.py:724`), `_number_lines` (`tools.py:599`), `_require`
(`tools.py:624`).

Two orderings are load-bearing and must survive verbatim:

- `_read_file` **numbers lines then truncates** (`tools.py:826`). Reversing
  renumbers. This is the recorded fix for colleague issue #240, where a served
  model cited a line ~240 off.
- `ToolExecutor.execute` wraps every non-`ToolError` exception into a recoverable
  model-visible error (`tools.py:788-800`). Without an equivalent, a handler crash
  aborts the drive instead of becoming a retryable step.

Schema byte-equivalence has concrete traps: `SCHEMAS[:6]` is a contiguous slice
(`tools.py:113-235`), but `_PATH_DESC` (`tools.py:61`) is interpolated into four
of the six descriptions and must travel verbatim; `list_dir` uniquely has **no**
`required` key; `read_file`'s description embeds literal backslash escapes. A
serialized-JSON snapshot test is the only honest guard.

### Policy extraction is nearly free

`colleague/policy.py:79-351` — `Verdict`, `Policy` and all its methods,
`file_checksum`, `verify_checksum`, `_first_token`, `_parse_policy_file`,
`_str_list`, `_str_map` — is ~270 lines of dependency-free stdlib needing **zero
changes**. Only `load_policy:354-400` couples, to `configdir.resolve_file` and
`layers.sanitize_model`. The cleanest seam passes **pre-resolved candidate paths**
so policy stops knowing about config-dir layout entirely, satisfying "shell core
accepts explicit policy data/files and has no `.colleague` import" without
injecting two callables.

Three no-op invariants the colleague tests will not let you break:

- an absent policy section is **ungated**, not empty-and-denying (presence, not
  emptiness, is the semantic);
- a malformed policy file degrades to `{}` and **never raises**;
- an empty policy is byte-identical to no policy — same step shape. Note the
  invariant is pinned at the **loop** level, over `TaskResult.to_dict()`, by
  `test_empty_policy_result_shape_is_byte_identical`. colleague's `Policy` has
  **no `to_dict()` method** — an earlier draft of this file claimed "same
  `to_dict()` key set" and was wrong about where the guarantee lives. shell-cli's
  own `Policy` *does* implement `to_dict()` (evidence needs it), with a fixed key
  set across empty/populated/degraded.

Absent policy and *malformed configured* policy must remain distinct states.
Native policy must never silently turn a malformed declared gate into allow-all.

**Policy does not gate only `run_command`.** The split is `check_file` × 2 and
`check_run_command` × 2:

- `check_file` — hooks (`hooks.py:311`, before every hook entry, every event) and
  commands (`commands.py:291`).
- `check_run_command` — the tool gate (`loop.py:903`) and the escalation gate
  (`escalation.py:176`, `check_run_command("agtag escalate")`).

An earlier draft listed `escalation.py:176` as a third `check_file` site. It is
not, and the difference is load-bearing for Milestone 3 scoping: the escalation
gate is a **command** gate, so it inherits the shlex-token weakness rather than
the checksum path.

File *tool calls* are deliberately not gated — pinned by
`tests/test_loop_run_command_policy.py:366-386`.

The policy **file format** is `.colleague/approvals.json`: three recognized
sections (`run_command` with `allow`/`deny` string lists, `hooks` keyed by
repo-relative path, `commands` keyed by stem), approval strings `<algo>:<hex>`
over `sha256`/`md5`, no `version` field in v0. Per-model overlays resolve at
`<sanitize_model(model)>/approvals.json` and replace **whole sections**, never
deep-merge. Sibling model dirs are never globbed, so one model cannot load
another's policy. Files exist in the wild; the `hooks`-are-paths vs
`commands`-are-stems convention is not self-describing and is the one place a
divergent format would silently mis-key rather than error.

### The enforcement ordering (and a discrepancy)

The gate sits in `colleague/loop.py:_run_tool_call` (`:914-1033`):

```text
pre_tool hook (deny/rewrite) -> policy -> execute -> record -> post_tool
```

**The single most security-relevant line in the extraction** is `loop.py:962`,
which re-wraps as `ToolCall(call.id, call.name, arguments)` so the policy sees the
**rewritten** arguments. An extraction that passes the original `call` silently
reintroduces a rewrite-bypass hole: a hook could rewrite a denied command into an
allowed shape.

Note a discrepancy with issue #1 §7, which mandates preserving `role gate ->
pre-tool hook/rewrite -> operation policy -> execution`. **There is no role gate
inside that sequence.** Role restriction lives in two places outside it: schema
curation before the model ever sees a tool (`tools.py:570-591`), and executor
dispatch as the first line of `execute` (`tools.py:757-761`), which runs *after*
the policy gate. Reconcile this before "preserving" an ordering that does not
exist.

### The subprocess inventory is tractable

Do not trust a hand-written inventory here — one was written for this file and
was already wrong. Run the scanner:

```bash
python3 scripts/colleague_inventory.py /path/to/colleague          # report
python3 scripts/colleague_inventory.py /path/to/colleague --check  # drift check
```

Against colleague 1.51.0 at commit `28fee290c51fc4310b9fc576981809ad5c3132c6`
it reports **21 process-spawn literals across 15 modules** — 6 `project`, 15
`control`, 0 owned by `observe` — with two `shell=True` sites
(`hooks.py:405`, `tools.py:1022`), plus ~40 filesystem-mutation sites that are
genuine runtime-private bookkeeping confined to `.colleague/`.

**Read those numbers for what they are.** They describe what a static AST scan
can see in source that spells its calls plainly. `shell=True` detection in
particular requires a literal `ast.Constant`, so "two `shell=True` sites" is a
statement about how colleague spells its arguments at this commit, not a
measurement of how often it shells out. `shell=SH` or `**{"shell": True}` would
report nothing.

An earlier hand survey of this repo reported *16* modules. It was wrong: the
AST scan shows the subprocess-importer set and the spawn-literal set are
identical at 15. That error survived a day in committed guidance, which is why
the count is now derived rather than transcribed.

There are **zero** `os.system`, `asyncio.create_subprocess_*`, `os.popen`,
`os.exec*`, and zero docker/podman invocations. Milestone 0 should close those
explicitly as vacuously satisfied rather than silently.

The scanner's `ALLOWLIST` is a **known-debt allowlist**: it records every module
permitted to spawn today, tagged with its profile and a debt flag, so the known
paths are tracked as scheduled migrations. Debt starts at **13 modules and must
reach zero by the end of Milestone 3** — a debt entry is a scheduled migration,
never a permanent exemption. `colleague/tests/test_boundary.py` already pins the
importing-module set on colleague's side.

**The scanner is a drift detector, not an enforcement gate — do not describe it
as one.** An earlier version of this file, of `t73`, and of the issue #1 §17
comment all claimed a new unclassified path would fail CI immediately. An
adversarial live test landed **30 executed evasions at exit 0** and that claim
is retracted; the full inventory is
[issue #7](https://github.com/agentculture/shell-cli/issues/7). The one that
matters architecturally: `ALLOWLIST` is keyed per **module**, not per **site**,
so a brand-new `shell=True` spawn added to any of the 15 already-allow-listed
modules — `tools.py` included — passes green with no signal. Widening the
detected-call set does not fix that; per-site pinning would.

Read a green `--check` as *"the inventory has not drifted in the ways this
scanner can see"*, never as *"no new unmediated spawn path was added"*.

Ambiguities worth adjudicating rather than assuming:

- `lint.py:100-102` runs `ruff check --fix` / `ruff format` — it **mutates tracked
  source** while wearing a gate's clothing, in deliberate contrast to
  `tools.py:1213-1219`, which keeps `run_tests` byte-neutral. Its mutations must
  surface as changed-path effects.
- The two `shell=True` sites have different trust stories and warrant different
  mediation: `tools.py:1022` runs a model-authored string; `hooks.py:405` runs a
  repo-authored hook **around every tool call**, including control-plane ones.
- `background.py:139` and `experiment.py:440` spawn detached
  (`start_new_session=True`), and `background.py` re-invokes colleague itself — so
  it re-enters every bucket in a process the mediator no longer supervises.

## Delivery plan

Implement in independently reviewable **vertical slices**. Do not ship one giant
rewrite.

- **Milestone 0 — inventory and characterization.** Inventory every
  process/workspace mutation path; classify each as `project`, `control`,
  `observe`, or not-a-shell-operation; explicitly account for runtime-private
  bookkeeping; snapshot the six tool schemas and observable behaviour; establish
  cross-repo compatibility fixtures.
- **Milestone 1 — operation core + parity provider.** Implement `Operation`,
  `OperationResult`, `Environment`, policy, evidence, confined filesystem
  operations, and `HostRunner`. Expose the six compatibility schemas. Keep
  pure-stdlib and preview-by-default. **Do not remove colleague's implementation.**
- **Milestone 2 — colleague delegates the six tools.** Compose shell-cli into
  colleague's existing router; **do not subclass it.** Preserve roles, hooks,
  policy ordering, result mapping and accounting. Run old/new characterization in
  parallel tests. Publish, pin a tested floor, then remove duplication.
- **Milestone 3 — route all colleague local operations.** Project execution
  (`run_tests`, lint, affected tests, project hooks); control execution (git,
  worktrees, handoff, neighbour cloning, AgentCulture CLIs); add the boundary test
  preventing new unclassified subprocess paths.
- **Milestone 4 — container runner.** Docker/Podman backend with the declared
  isolation profile; project profile uses it when selected; colleague's worktree
  becomes the mounted work root; dependency preparation and sealed execution are
  distinct operations; host fallback is explicit and visible, never silent.
- **Milestone 5 — ecosystem providers.** Publish the stable capability/evidence
  contract; integrate `webglass-cli` as a separate semantic provider.

Container baseline when Milestone 4 lands: non-root user mapped to host UID/GID;
no privileged mode, no Docker socket; all capabilities dropped unless restored;
network disabled by default; read-only container root; only the work root mounted
writable; explicit dependency/cache volumes; bounded CPU, memory, pids, output,
wall time; explicit redacted secret injection. Dependency preparation may be a
separate network-enabled operation, and must not quietly enable network during
sealed execution.

A linked git worktree holds a `.git` **pointer** into the source repo's common
git dir, so mounting only the worktree does not preserve in-worktree git
behaviour. Milestone 4 must choose explicitly: either project containers get no
git metadata (structured git runs through the control plane), or the environment
receives isolated git metadata that cannot mutate unrelated host refs. **Do not
solve this by mounting the host common `.git` writable** — that hands project code
a path back to operator refs.

### Compatibility invariants

The colleague migration succeeds only if colleague loses no behaviour.

- The six OpenAI tool schemas stay byte-equivalent, including order and
  descriptions, until an intentional separately-reviewed change.
- Role curation and runtime refusal remain intact.
- Hook rewrite precedes command policy.
- Policy denial remains a non-success step with the same model-visible reason and
  telemetry meaning.
- `write_file` / `edit_file` still apply immediately through the adapter.
- `run_command` remains a fresh shell, rooted cwd, bounded timeout, current output
  rendering during parity.
- Line numbering, truncation, media size/type limits, and error messages stay
  compatible.
- Changed-file aggregation, subagent changes, and `bytes_written` ROI accounting
  remain intact.
- Worktree/self-commit/continuation/merge/handoff behaviour remains intact.
- Malformed arguments remain recoverable model-visible tool errors, never run
  aborts.

Characterization tests must compare legacy and new execution paths against the
same fixtures **before** the legacy implementation is removed.

### Test ownership

Tests move by responsibility, not filename.

**shell-cli owns**: operation lifecycle and result states; filesystem confinement
and symlink escapes; read/write/edit/list/media behaviour; `HostRunner` and
`ContainerRunner`; policy parsing/evaluation; preview/apply semantics;
evidence/redaction/truncation; generic git/worktree primitives; zero-dependency
and import cleanliness.

**colleague retains**: role schema curation and runtime refusal; hook
ordering/rewrite/deny; all-engine policy parity; `ToolOutcome` and media-message
adaptation; changed/subagent/ROI aggregation; worktree branch naming, continuation
and handoff; loop recovery, telemetry, validation gates, artifact parity.

Worth mining from the colleague checkout's `tests/`:
`test_loop_run_command_policy.py`, `test_policy_carveout.py`,
`test_policy_all_engines.py`, `test_write_apply_isolation.py`,
`test_zero_deps.py`, `test_boundary.py`, `test_tools.py`, `test_view_media.py`,
`test_tool_arg_errors.py`.

## Before large-scale work

Issue #1 §17 gates implementation behind a concrete Milestone 0/1 plan, posted as
an issue reply, naming six things:

1. the proposed operation/result/environment types;
2. the exact colleague characterization tests;
3. how policy ordering and role refusal remain unchanged;
4. how state/accounting is mapped without importing colleague;
5. which direct subprocess paths are parked for Milestone 3;
6. the smallest first PR that proves the seam without duplicating a framework.

Use devague (`/challenge`) to pressure-test that plan for boundary leaks, false
safety claims, version skew, and operations that would bypass the selected
environment. That pass is expected, not optional.

## Open questions — park them, do not guess

Issue #1 settles several questions an earlier draft of this file left open:
`view_media` **travels** here; the policy **evaluator** moves while **location
resolution** stays; read-only paths **are** generalised; `shell policy
check|explain` **is** a first-class verb. Do not reopen those.

These remain genuinely open:

- **stdout/stderr shape.** §10 requires preserving `run_command`'s current
  rendering, but colleague concatenates stdout and stderr unlabelled into one
  string (`tools.py:1047-1048`), while §8 requires them captured **separately**.
  Capturing separately and letting the adapter concatenate satisfies both — but it
  means the neutral result and the compat rendering diverge from day one.
- **The bookkeeping exemption has two leaks.** The approvals ledger
  (`cli/_approvals.py:42,59`) lives in the exempt `.colleague/` directory but *is*
  the authorization surface. Skill files (`learn_from.py:431-444`) are written from
  another agent's repository and folded into every backend's system prompt.
  "Executes code or touches the workspace" catches neither, because influencing
  authorization and influencing the prompt are separate categories.
- **Detached processes.** Does a detached re-invocation count as one classified
  control operation, or must the child inherit the selected environment? A wrapper
  on the synchronous path is bypassable by construction.
- **Policy file format.** Stay byte-compatible with colleague's `approvals.json`,
  or define a shell-native format with a compatibility reader?
- **Second consumer.** Does anything beyond colleague adopt this soon, and should
  that shape the API now or wait?

## Definition of done

- Every colleague work-affecting local operation is inventoried and classified;
  runtime-private bookkeeping is explicitly accounted for.
- Every project-code execution path uses the selected shell environment.
- Every trusted control subprocess uses an explicit control profile.
- colleague has no unclassified direct subprocess path.
- The six original tools migrate with observable parity.
- Existing worktree and handoff guarantees remain green.
- Host mode is documented as a guard, never a sandbox.
- Container mode's actual isolation profile is tested and recorded per operation.
- Structured evidence is available to colleague, humans, telemetry, and validators.
- Base shell-cli remains pure-stdlib.
- The CLI and library use the same operation engine.
- colleague imports shell-cli; shell-cli never imports colleague.
- A migration proposal is **filed as an issue on colleague** — how it drops its
  copy, allow-lists `shell-cli` in its guard test, and keeps behaviour identical.
  colleague's change is colleague's to make: **propose, don't push.**

## Non-goals

- reasoning, planning, roles, or agent orchestration
- model APIs or prompt construction
- web search/navigation semantics (`webglass-cli` owns them)
- memory semantics (`eidetic-cli` owns them)
- devague/culture/coherence domain logic
- PR policy, acceptance judgment, or handoff decisions
- claiming containers are perfect isolation, or that worktrees contain anything
- adding abstractions with no current colleague or CLI consumer

### The WebGlass peer seam

`webglass-cli` is a **peer, not a layer** — a separate repository and a separate
colleague capability seam, on the same footing as shell-cli rather than beneath
or above it. colleague composes both; neither composes the other.

The boundary is drawn at semantics. A browser session, a page fetch, a
navigation step and a search result are **web** semantics and belong entirely to
webglass-cli. They are not local operations and must never be modelled as
`Operation` kinds here — a `web.fetch` kind in this package would be the seam
collapsing.

**Only provider-neutral artifacts cross.** When webglass produces a file, it is
just a file: shell-cli can be asked to read it through the ordinary confined
`fs.read` path, with no knowledge that a browser produced it and no webglass
type in the signature. `tests/test_boundaries.py` asserts shell-cli's import
graph contains no webglass module, in the same breath as the colleague check.

Scope consequence: WebGlass integration is Milestone 5 work, planned in its own
repository against its own spec. It is deliberately **not** covered by this
repo's Milestone 0/1 plan, and no task here delivers it.

## Naming, and the prompt-file story

- **`shell` is the executable; `shell-cli` is the repo, the agent nick, and the
  PyPI distribution.** Both are correct in their own place, so neither is a
  find-and-replace target. Usage examples, `prog`, and anything a reader might
  paste into a terminal say `shell`. The nick reported by `whoami` (from
  `culture.yaml`'s `suffix`), the distribution name, the Sonar project key, and
  the GitHub URLs stay `shell-cli`. `explain` accepts both as the root path.
- **Prompt files.** `culture.yaml` declares `backend: colleague`, so `doctor`'s
  backend-consistency check requires `AGENTS.colleague.md` — which exists and
  passes. This `CLAUDE.md` is the Claude Code prompt and the contributor guide; it
  does **not** change that mapping. Keep both files, and keep them from
  contradicting each other.

## Conventions and workflow

- **Skills**: `.claude/skills/` is vendored cite-don't-import from guildmaster
  (and directly from devague/colleague for a few tracked divergences). Provenance
  and the re-sync procedure live in `docs/skill-sources.md` — read it before
  editing anything under `.claude/skills/`; local edits are lost on re-sync unless
  lifted upstream first. Vendored skills are excluded from markdownlint and from
  Sonar analysis on purpose.
- **PR lane**: use the `cicd` skill (`devex pr` plus SonarCloud `status` /
  `await`). CI blocks on the Sonar quality gate when `SONAR_TOKEN` is set.
- **Second opinion**: reach for `ask-colleague` reflexively — `review` before
  opening a PR on a non-trivial diff, `explore` for a fresh read of an unfamiliar
  area. Both are read-only and run in a throwaway worktree, so the reflex is always
  safe; the side-effecting `write --apply` / `--pr` needs the user's go-ahead.
  Given that colleague is *the* consumer of this package, its read of an extraction
  seam is worth more here than usual.
- **Memory**: `/recall` before non-trivial work to build on prior decisions rather
  than re-deriving them; `/remember` when a non-obvious decision, constraint,
  fix-and-why, or hard-won gotcha surfaces. This repo's memory is in-repo and
  public — records resolve to `<repo-root>/.eidetic/memory` (committed, team- and
  mesh-shared). Pass `--visibility private` to route a record to `$HOME` instead.
- **Cross-repo**: use the `communicate` skill to file issues on sibling repos
  (auto-signs `- shell-cli (Claude)`); use `gh issue create` or `cicd` for issues
  on this repo.
