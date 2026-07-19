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
| A new unclassified subprocess path in colleague will fail shell-cli's CI | `inventory-gate` green in CI; synthetic fixture with an unclassified module exits 1; allow-listed-only fixture exits 0 | **High** — both directions pinned by test |
| colleague has 21 spawn sites across 15 modules, 13 of them debt | Scanner output at pinned SHA `28fee29`, asserted by the CI `sha_matches` guard | **High** — mechanically derived, not transcribed |
| Every guidance surface now describes the operations plane | README, AGENTS.colleague.md, CLAUDE.md rewritten; `tests/test_honesty.py` green | **Medium** — no test asserts *framing*, only the honesty posture |
| The extraction seam is proven | — | **None. Not claimed.** No `Operation` type exists yet. |

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
4. Upstream follow-ups found during this run: colleague tool-calling failure
   (`d1`), `devex pr open --body-file -` traceback, and the `devague summary`
   rejected-task defect.

## Process Notes

`t73` was built by a subagent in an isolated git worktree and merged under a TDD
gate — 49 tests green before the merge, 66 after — then the worktree was reaped.
`t71` and `t72` were implemented directly, being outward-facing (publishing
pipeline) and issue-posting work respectively.

Not every planned mechanism survived contact: the approved split plan expected
colleague to implement five tasks and review every diff, and it did neither.
