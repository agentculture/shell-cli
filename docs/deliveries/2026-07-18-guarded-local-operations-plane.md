# Delivery Summary — guarded local operations plane

plan: `guarded-local-operations-plane` · run: **partial (blocked)** · date: `2026-07-19`
baseline: `.devague/plans/guarded-local-operations-plane.json` (19 confirmed tasks)

## Intent

shell-cli is the guarded local operations plane for AI agents: every
work-affecting local operation is normalized into one `Operation`, evaluated
against an operator policy snapshot, executed in an explicitly selected
environment, and returned with structured evidence. colleague decides *why*,
*when*, and *which*.

This run executed the **Milestone 0 opening slice**. It did not reach Milestone 1,
and no operation, environment, policy, or runner has been built.

## Actual Delivery

Only confirmed tasks are listed. The plan's 68 rejected tasks from earlier
iterations are excluded — `devague summary` currently renders them as planned
work, which is a devague defect, not a record of intent.

| Plan task | Status | What actually landed |
|---|---|---|
| `t71` | **delivered** | PR #3, merged. `publish.yml` gains `gates` + `smoke`; both publish paths depend on `[test, gates, smoke]`. `docs/release-runbook.md`. Released 0.8.0. |
| `t72` | **delivered** | PR #5, open and green. README/AGENTS/CLAUDE realigned to the operations-plane framing; WebGlass peer seam documented; devague artifact trail committed; six-part plan posted on #1. |
| `t73` | **delivered** | PR #5, open and green. Scanner tracked; `inventory-gate` CI job live; 17 synthetic-fixture tests. |
| `t69`, `t70` | **not started — not ours** | colleague-repo security lane. Not filed. See *Blocked* below. |
| `t74` | **blocked** | Requires a fixed colleague release (deps `t69`, `t70`). |
| `t75`–`t85` | **blocked** | Transitively blocked behind `t74`. |
| `t86`, `t87` | **blocked — not ours** | colleague-repo M2 cutover; additionally depends on all of M1. |

**3 of 19 confirmed tasks delivered. 12 blocked. 4 belong to another repository.**

## Delivery Claims (with evidence)

Claims are stated at the strength the evidence supports, and no higher.

| Claim | Evidence | Confidence |
|---|---|---|
| A release can no longer publish while any quality gate is red | `publish`/`test-publish` now `needs: [test, gates, smoke]`; all three observed passing on PR #3 and #5 in real CI | **High** — verified in CI, not just written |
| The published wheel imports, exposes the `shell` console script, and declares zero runtime dependencies | `smoke` job green in CI; also live-tested locally against a real built wheel before commit | **High** |
| A new unclassified subprocess path in colleague will fail shell-cli's CI | **Retracted.** An adversarial live test landed 30 executed evasions at exit 0 | **None — this claim was false and is withdrawn.** See *Adversarial live test* below |
| The scanner reproduces a pinned inventory and detects drift from it | Reproduces 21 / 15 / 13 at SHA `28fee29` exactly; `sha_matches` guard asserts the tree | **High** — this is what the tool actually does |
| colleague has 21 spawn sites across 15 modules, 13 of them debt | Scanner output at pinned SHA `28fee29`, asserted by the CI `sha_matches` guard; unchanged after the alias-detection fix | **High** — mechanically derived, not transcribed |
| Every guidance surface now describes the operations plane | README, AGENTS.colleague.md, CLAUDE.md rewritten; `tests/test_honesty.py` green | **Medium** — no test asserts *framing*, only the honesty posture |
| The extraction seam is proven | — | **None. Not claimed.** No `Operation` type exists yet. |

## Post-merge review found four real defects

The most important thing this run learned. After `t73` merged, Qodo posted four
findings against work that had passed the full local suite, all CI gates,
SonarCloud's quality gate, and a TDD merge gate. All four were legitimate and
all four are fixed.

| # | Defect | Why the gates missed it |
|---|---|---|
| 1 | `README` asserted container isolation for an unbuilt runner | `test_honesty.py`'s `_CLAIM` regex matches "sandbox" and "fully isolated" — it cannot see a table cell reading "Execution isolation" |
| 2 | Alias imports evaded the scanner (`import subprocess as sp`) | Every test used plain `import subprocess`; the tests shared the author's blind spot |
| 3 | A scan failure was fail-open — an unparseable file counted as "no spawns" | A test asserted the file was skipped *without crashing*, which is the wrong property to assert of an enforcement tool |
| 4 | `ALLOWLIST` matching broke on Windows separators | CI runs Linux only |

Defect 2 is the one that matters. The gate's entire stated value is that a new
unmediated spawn path cannot land unnoticed — and for the window between merge
and fix, `import subprocess as sp; sp.run(...)` would have walked straight
through it. The pinned counts did not change, because colleague 1.51.0 uses
plain `import subprocess` throughout: **the numbers were honest, the guarantee
was not.** A latent bypass, not an active under-report.

Two durable responses rather than four point fixes:

- the honesty suite now guards the *framing* of the environment matrix (every
  row must declare whether it is built), not just the word "sandbox" — and both
  new tests were verified to fail against the table as previously shipped;
- fixing defect 2 also removed a mirror-image false positive nobody had
  reported: `import mything as os` followed by `os.system(...)` was being
  counted.

Tests grew 66 → 92 across the fixes.

## Adversarial live test — the delivered gate does not do what it claimed

After the Qodo fixes landed, the work was live-tested by three independent
minds. One of them broke it.

**An adversarial pass produced 30 executed evasions at exit 0 and 6 false
positives.** Every evasion fixture was run and observed to create a real
process. Full inventory: issue #7.

The claim *"a new unclassified subprocess path in colleague will fail
shell-cli's CI"* — made in `t73`, in the issue #1 §17 comment, and in the first
draft of this document — **is retracted**. Three findings defeat it:

- **`ALLOWLIST` is keyed per module, not per site.** Three brand-new spawns
  added to `tools.py` — two `shell=True`, running `rm -rf` and
  `cat /etc/shadow` — returned exit 0 with no signal. 15 modules are already
  allow-listed, so a new unmediated path in any existing file is invisible **by
  design**. This is architectural, not a missing case.
- **`subprocess.getoutput("cmd")` defeats it in one line.** `_SPAWN_CALLS` is a
  12-entry literal set; ~15 real spawn APIs are absent.
- **One level of indirection defeats resolution.** `sp = subprocess; sp.run(...)`
  passes, so the alias hardening from the Qodo fixes covers only `import ... as`.

What the tool actually does — reproduce a pinned inventory exactly and notice
when it drifts — it does well, and the four honest baselines all pass. So the
response was to relabel rather than overclaim-and-patch: the scanner docstring,
the CI job comment, `CLAUDE.md`, this document, and the issue #1 comment now all
describe a **drift detector against a pinned baseline**, never an enforcement
gate. The structural fixes are scoped in #7 as their own reviewed slice, because
they change the pinned baseline and the tool's contract.

The honest ceiling is the same posture this repo already commits to for the
execution guard: it catches accidental and careless drift, not adversarial
evasion. The tooling should promise no more than the product does.

## Clean-room live test — the documentation is executable

A second mind, given only the written docs and forbidden from using outside
knowledge to patch over gaps, reproduced the whole surface: **clean pass, zero
documentation defects.** Every README Quickstart command ran verbatim; all six
CLI verbs and their `--json` twins behaved as documented; stdout/stderr never
mixed and no error path leaked a traceback; the release runbook's gate chain
matched `publish.yml` line for line; and the `smoke` job reproduced by hand in a
genuine `python3 -m venv` — wheel installs, declares zero runtime dependencies,
`import shell` resolves to the installed package, and `bin/shell` exists while
`bin/shell-cli` does not.

One real gap: **exit code 2 is documented policy but currently unreachable.**
`EXIT_ENV_ERROR` is defined in `shell/cli/_errors.py` with zero call sites, so
no CLI command can produce it today. Not false — the docs phrase it as policy,
not a per-command guarantee — but a newcomer verifying the exit-code contract
can exercise 0 and 1 and never 2.

## Third mind — colleague

Reported under *Drift From Plan* (`d1`). It contributed nothing to this run.

## Mid-work Decisions

- **Version bumps moved from task agents to the merging agent.** The plan's
  global invariant put "version bumped" on every task, which would have made
  `pyproject.toml` and `CHANGELOG.md` a guaranteed conflict in every multi-task
  wave. Task agents now skip both; the merging agent bumps once per merge. The
  invariant still holds where CI checks it — at PR level.
- **`t73` gained a `sha_matches` assertion not in its acceptance criteria.**
  Without it the gate could pass green against a colleague tree other than the
  pinned one. Added by the task agent, kept.
- **`colleague_inventory.py` exit-code change.** A missing or unreadable
  checkout now reports an environment error (exit 2) rather than raising
  `SystemExit`, so CI can distinguish a broken clone from a real gate failure —
  previously both exited 1.

## Drift From Plan

Three deviations recorded in devague's append-only ledger, `proposed` and
awaiting operator `--confirm`. Human-readable record: issue #4.

- **`d1` (needs-follow-up)** — colleague dropped from the implementer and
  reviewer roles. Two consecutive drives on read-only tasks completed **zero
  steps**, emitting tool calls as literal assistant text. Claude subagents
  implemented instead; SonarCloud and Qodo supplied independent review.
- **`d2` (acceptable)** — `t72`/`t73` started from partially-complete work, since
  the `CLAUDE.md` rewrite and the scanner were authored during the spec phase.
- **`d3` (acceptable)** — PR ordering forced to strict sequence: every merge
  publishes, so `t71` had to merge alone and first.

## Blocked — and why it is not a workaround

The critical path stops at a dependency this repository cannot satisfy.

`t74` captures characterization fixtures from a **fixed** colleague. The fix is
`t69`/`t70`, which live in `agentculture/colleague` and are being handled as a
private security matter. They have **not been filed**. Per `c38`/`h57`,
colleague's changes are colleague's to make: propose, don't push.

Capturing fixtures from today's colleague would enshrine the defective behaviour
as the parity baseline — the one outcome the characterization step exists to
prevent. So the block is respected rather than worked around.

**The single action that unblocks 12 tasks is filing the colleague security
advisory.**

## Remaining Work

1. File the colleague security advisory (`t69`, `t70`) — privately, not as a
   public issue.
2. Once a fixed colleague is released: `t74` (fixtures), `t75` (drift gate).
3. Milestone 1 proper, beginning with `t76` — the foundation slice that ships
   `Operation`/`OperationResult`/`Environment` together with the zero-dep guard,
   the honesty gate, and the negative-import check.
4. **Harden or retire the inventory scanner (#7).** Per-site pinning is the only
   fix for the module-granularity hole; widening the detected-call set and
   making binding resolution scope-aware close the rest. Until then the tool
   stays labelled a drift detector.
5. Make exit code 2 reachable, or stop documenting it as part of the CLI's
   exit-code policy. `EXIT_ENV_ERROR` currently has zero call sites.
6. Upstream follow-ups found during this run: the colleague tool-calling failure
   (`d1`); `devex pr open --body-file -` raising a traceback instead of reading
   stdin; `devex pr reply` accepting `"resolve": true` and silently resolving
   nothing (reported 4 replies / 0 resolved / 0 failures — the threads had to be
   resolved via GraphQL); and the `devague summary` rejected-task defect
   (devague#88).

## Process Notes

`t73` was built by a subagent in an isolated git worktree and merged under a TDD
gate — 49 tests green before the merge, 66 after — then the worktree was reaped.
`t71` and `t72` were implemented directly, being outward-facing (publishing
pipeline) and issue-posting work respectively.

Not every planned mechanism survived contact: the approved split plan expected
colleague to implement five tasks and review every diff, and it did neither.
