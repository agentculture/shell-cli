# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.7] - 2026-07-19

### Changed

- Honesty guard: the environment-table row selector is now structural (rows after the pipe-table header separator) instead of matching the strings Host or Container. The old filter would have silently matched nothing — and so asserted nothing — the moment a runner was renamed. Verified to fail against a table whose runner column reads Firecracker.
- colleague_inventory: corrected a misleading fail closed comment on the unbound-attribute fall-through. The bound and unbound cases are not in tension, but the fall-through is a known false-positive source (issue #7), not a safety property.

## [0.8.6] - 2026-07-19

### Changed

- Delivery summary: defined the status vocabulary (released / submitted / blocked / not ours) and corrected t72/t73 from open to merged; clarified that the deviation classifications are agent-proposed, not operator-confirmed; explained that the filename date is the plan creation date per the devague lane convention, not the run date.

## [0.8.5] - 2026-07-19

### Changed

- colleague_inventory is now described honestly as a DRIFT DETECTOR against a pinned baseline, not an enforcement gate. An adversarial live test landed 30 executed evasions at exit 0 (issue #7); the claim that a new unclassified spawn path fails CI is retracted in the scanner docstring, the CI job comment, CLAUDE.md, and the delivery summary.
- CLAUDE.md now qualifies the shell=True figure: detection requires a literal ast.Constant, so two shell=True sites describes how colleague spells its arguments at the pinned commit, not how often it shells out.

## [0.8.4] - 2026-07-19

### Added

- Two honesty tests guarding the framing of the environment matrix, not just the word sandbox — the previous guard could not catch a table cell reading Execution isolation.

### Fixed

- colleague_inventory: alias imports no longer evade the scanner. import subprocess as sp; sp.run(...) and from subprocess import run; run(...) were both invisible to the literal dotted-name match, so a new unmediated spawn path could have landed without failing the gate. Import bindings are now resolved before matching. Also removes the mirror-image false positive, where import mything as os; os.system(...) was wrongly counted.
- colleague_inventory: a scan failure is no longer fail-open. An unparseable or unreadable file was silently treated as containing no spawns, so --check could pass while part of the checkout went unscanned. Skipped files are now recorded, published in the JSON and text output, and force exit 2 — an untrustworthy scan has no verdict to give.
- colleague_inventory: allowlist matching is platform-independent. Module keys are normalised with as_posix(), so resident/steward.py matches on Windows instead of reporting a false unclassified path.
- README: the environment matrix no longer implies isolation that does not exist. Every row is marked as a design target with an explicit Built? column, all No today.

## [0.8.3] - 2026-07-19

### Added

- docs/deliveries/2026-07-18-guarded-local-operations-plane.md: the delivery summary closing the devague loop for the Milestone 0 opening slice — planned versus actual, mid-work decisions, the three recorded deviations, and delivery claims stated at the strength the evidence supports.

## [0.8.2] - 2026-07-19

### Added

- scripts/colleague_inventory.py is now tracked: a pure-stdlib AST scanner that inventories colleagues process-spawn paths, pinned to colleague SHA 28fee29 (1.51.0).
- CI inventory-gate job: clones the public colleague repo at the pinned SHA, asserts it scanned that exact commit, publishes debt_remaining to the step summary and as a notice on every run, then fails on any unclassified spawn path.
- tests/test_colleague_inventory.py: 17 tests driving the scanner against synthetic fixtures, so the gate is provable in CI without a colleague checkout.

### Changed

- colleague_inventory.py now reports a missing or unreadable checkout as an environment error (exit 2) rather than raising SystemExit, so CI can distinguish a broken clone from a real gate failure.

## [0.8.1] - 2026-07-19

### Added

- docs/specs/ and docs/plans/: the converged guarded-local-operations-plane spec and its buildable plan, plus the external review record under docs/external-review/.
- scripts/render_plan.py: generates the plan projection from authoritative devague state, with a SHA-256 drift check (--check). Temporary until devague#85 lands deferred-target semantics.
- CLAUDE.md: an explicit WebGlass peer-seam section — webglass-cli is a peer capability, web semantics never become Operation kinds here, and only provider-neutral artifacts cross.

### Changed

- README.md rewritten to the operations-plane framing: the operation lifecycle, the two-axis environment model, the three profiles, evidence as a product surface, and an explicit non-goals section. Replaces the superseded extract-six-tools-from-colleague framing.
- AGENTS.colleague.md now states what this repo is and the contracts a resident can break by accident (pure-stdlib core, guard-not-a-sandbox, preview-by-default, version-per-PR, no tracebacks).

## [0.8.0] - 2026-07-19

### Added

- Release runbook (docs/release-runbook.md): the gate chain, the yank-and-fix-forward procedure, the colleague pin policy, and the security-fix exemption from the migration-proposal gate.
- publish.yml: a gates job mirroring the full lint suite (black, isort, flake8, bandit, markdownlint, teken rubric), and a smoke job that installs the built wheel into a clean venv, asserts it declares zero runtime dependencies, and runs its console script.

### Changed

- publish.yml: the publish and test-publish jobs now depend on test + gates + smoke, so a release can no longer go out while any quality gate is red. Previously publish gated on pytest alone.

## [0.7.2] - 2026-07-18

### Fixed

- **The honesty guard no longer fires on honest text.** `tests/test_honesty.py`'s overclaim pattern matched negated phrasing — `"This is not fully isolated"`, `"Do not treat this as a sandbox"`, and `"It is never sandboxed"` all tripped it. A guard meant to catch overclaiming would have failed CI on wording that states the disclaimer *more* strongly, inverting its purpose. Matching is now negation-aware: candidates are discounted when a negator precedes them **in the same sentence**, so a disclaimer cannot excuse a genuine claim in the next sentence.
- **Guard failures now name the offending text.** The pattern used capturing groups with `re.findall()`, so a failure reported tuples of fragments (`[('', '', '')]` for the `fully isolated` branch) instead of the matched phrase. Groups are now non-capturing and matching uses `finditer()` + `group(0)`. Also anchored `sandbox\b` so a match inside `sandboxed` reports `"sandboxed"` rather than the truncated `"is sandbox"`.
- Added guard-the-guard coverage in both directions — 8 honest phrasings that must not trip it, 5 affirmative claims that must, plus sentence-boundary and failure-message regressions (23 new cases). Both findings raised by Qodo on PR #2.

## [0.7.1] - 2026-07-18

### Fixed

- **`test_learn_text` no longer asserts a vestigial substring.** It checked `"shell-cli" in out`, which after the executable rename passes only incidentally via the GitHub issue URL in the `learn` body — the assertion would have survived the entire command map being deleted. It now asserts `"shell whoami"`, tying the test to the command map it is meant to cover. Found by a `colleague` review run (see agentculture/colleague#353).

## [0.7.0] - 2026-07-18

### Added

- **`shell explain safety`** — the guard-not-a-sandbox threat model is now readable in-band, so an agent can query the posture before deciding what to trust rather than inferring it from the package name.
- **`docs/threat-model.md`** — assets and actors, a protected-vs-not table, the known bypasses (`sh -c`, shell expansion, here-docs, interpreters, symlink/TOCTOU), and what real isolation would actually require. Documents the contract the pending colleague extraction must uphold; the posture is inherited with the code and must not be upgraded in transit.
- **`tests/test_honesty.py`** — a guard test making the safety disclaimer load-bearing rather than droppable prose. Asserts the posture is present in `learn` (text and the new `safety_posture` JSON field), the `explain` root, `explain safety`, `README.md`, `CLAUDE.md`, and `docs/threat-model.md`, and that no shipped surface makes a positive isolation claim.
- **`CLAUDE.md`** — expanded from the self-init seed into a full runtime prompt (issue #1): the four non-negotiable constraints, the CLI contracts already enforced by tests, the verified extraction map into `colleague` 1.51.0 (handlers, safety helpers, and `policy.py` symbols with line numbers), the decoupling work that is the real difficulty, the planned VM/in-container backend, and the parked open questions.

### Changed

- **The CLI now names its own executable correctly.** argparse's `prog` was `shell-cli` while `[project.scripts]` installs `shell`, so `--help`, every `explain` body, and the `learn` text printed commands that do not exist (`uv run shell-cli whoami` failed outright). `prog`, all usage examples, the `cli overview` subject, and the `doctor` status line now say `shell`; `shell-cli` is retained only where it is genuinely the repo/PyPI distribution token. Both `explain shell` and `explain shell-cli` resolve to the root entry.
- **CLI self-description now describes this agent, not the template it was cloned from.** `learn`, `explain`, and `overview` called shell-cli "a clonable template for AgentCulture mesh agents" and listed template-onboarding artifacts. They now state the actual mission (the file-and-shell tool surface), the safety posture, and an honest Status section recording that the six primitives, path confinement, and approval policy are not extracted yet. `overview` gains Mission and Status sections; `learn --json` gains `tool`/`distribution`/`safety_posture`/`status` fields.
- **`README.md`** — rewritten around the mission, with the guard-not-a-sandbox warning promoted above the fold per the build brief, an explicit Status section, the two surfaces in priority order (library first), the four constraints, and a corrected quickstart.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `~/.eidetic/memory` surface, so this agent (Claude and its colleague backend)
  can persist facts across sessions and recall them later, sharing one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`shell-cli vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/shell-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `shell/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=shell` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/shell-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/shell-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: shell-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
