# Delivery Summary — guarded local operations plane (Milestone 1)

plan: `guarded-local-operations-plane` · run: **partial** · date: `2026-07-19`
baseline: `devague summary skeleton` (filtered to confirmed tasks — see note below)

**Dates.** The filename carries `2026-07-18`, the plan's creation date, matching
its siblings under `docs/specs/` and `docs/plans/` — the devague lane convention.
The run itself executed on **2026-07-19**. The `-m1` suffix distinguishes this
from the earlier M0 summary of the same plan.

**Baseline note.** `devague summary` renders the plan's 68 **rejected** tasks
from earlier iterations as planned work — a devague defect, not a record of
intent, already recorded in the M0 summary. The Planned Work section below is
filtered to the 19 **confirmed** tasks (`t69`–`t87`) and quoted verbatim from
that skeleton.

## Intent

shell-cli is the guarded local operations plane for AI agents: every
work-affecting local operation is normalized into one `Operation`, evaluated
against an operator policy snapshot, executed in an explicitly selected
environment, and returned with structured evidence. colleague decides *why*,
*when*, and *which*.

This run executed **Milestone 1** — the operation core and the compatibility
primitives — after the M0 opening slice had landed. It did not reach Milestone 2.

## Planned Work

Quoted verbatim from the `devague summary` skeleton, `owns:` clauses elided:

- `t69` — [colleague / security-lane / PUBLISHES a fixed colleague release] Private security PR: resolve policy+hooks from the operator source root and carry that identity through nested worktrees and children — RESOLUTION ONLY, per-model overlay semantics unchanged.
- `t70` — [colleague / security-lane / separate review / publishes a colleague release] Harden child policy as a restriction of the parent: child_effective = parent_cap INTERSECT source_root_child_model_policy, deny winning and allowlists intersecting.
- `t71` — [shell-cli / M0 / PR1 — FIRST MERGED shell-cli PR / publishes scaffold-only release] Release and publishing hardening BEFORE any later merge publishes.
- `t72` — [shell-cli / M0 / PR2 / publishes scaffold-only release] Post the six-part Milestone 0/1 plan on issue #1, align every guidance surface with the operations-plane issue, and document the WebGlass peer-seam boundary.
- `t73` — [shell-cli / M0 / PR3 / publishes scaffold-only release] Commit the inventory scanner with its pinned SHA and wire --check into CI as a known-debt gate that publishes debt_remaining.
- `t74` — [shell-cli / M0 / PR4 / publishes scaffold-only release] Capture the FIXED colleague baseline: generate schemas and fixtures, build the provider-neutral harness interface, and prove fixture regeneration.
- `t75` — [shell-cli / M0 / PR5 / publishes scaffold-only release] Install the source-hash drift gate BEFORE extraction begins.
- `t76` — [shell-cli / M1 / PR6 / publishes] FOUNDATION: Operation, OperationResult, Environment with schema_version, the lifecycle pipeline, HostRunner skeleton — WITH the zero-dep guard, honesty gate and WebGlass negative-import check.
- `t77` — [shell-cli / M1 / PR7 / publishes] Evidence contract: effects-completeness marker, redaction scope, schema_version on persisted records, storage location, retention, degraded-evidence behaviour — tested against SYNTHETIC results.
- `t78` — [shell-cli / M1 / PR8 / publishes] Port the policy evaluator with no config-dir coupling, and snapshot policy from source_root with generalised read-only paths.
- `t79` — [shell-cli / M1 / PR9 / publishes] Evaluate policy INSIDE the operation dispatch on REWRITTEN arguments, so no caller can route around the gate.
- `t80` — [shell-cli / M1 / PR10 / publishes] HostRunner execution semantics: process groups, timeout and cancellation escalation, orphan-prevention honesty.
- `t81` — [shell-cli / M1 / PR11 / publishes] fs.read and fs.list, preserving number-then-truncate and the recoverable-error wrapper.
- `t82` — [shell-cli / M1 / PR12 / publishes] fs.write and fs.edit with byte accounting.
- `t83` — [shell-cli / M1 / PR13 / publishes] fs.media vendoring only the media slice while preserving the handler-level 4 MiB cap and images-only rule.
- `t84` — [shell-cli / M1 / PR14 / publishes] process.exec and process.shell with distinct profiles, separately captured stdout/stderr, and REAL effects/redaction integration.
- `t85` — [shell-cli / M1 / PR15 / publishes] Policy and operation CLI surface with honest scope reporting; library-versus-CLI equivalence proven with a REAL fs.read operation.
- `t86` — [colleague / M2 / PR1 / publishes a colleague release] Add the bounded shell-cli dependency and compose the adapter into colleague's router WITHOUT subclassing, proving colleague drives all six tools through shell-cli.
- `t87` — [colleague / M2 / PR2 / publishes a colleague release] Run differential parity — both implementations in one session over identical fixtures — then remove colleague's legacy implementation.

## Actual Delivery

All 19 confirmed tasks accounted for. `t71`–`t73` were delivered by the earlier
M0 run and are marked as such rather than re-claimed here.

| Plan task | Status | What actually landed |
|---|---|---|
| `t69` | **blocked** | Not ours to commit. Filed privately as GHSA-6pfg-9vcp-hqw4 against colleague, with the mechanism traced to `work.py:309` + `loop.py:4094-4096` and the fix pattern (`flight_repo_path`, colleague #310) named. |
| `t70` | **blocked** | Not ours to commit. Filed privately as GHSA-2m42-prxw-43w6. |
| `t71` | delivered (M0 run) | PR #3, 0.8.0 — publish gated on the full suite; `docs/release-runbook.md`. |
| `t72` | delivered (M0 run) | PR #5, 0.8.2 — guidance surfaces realigned; six-part plan posted on #1. |
| `t73` | delivered (M0 run) | PR #5, 0.8.2 — scanner + `inventory-gate`. Relabelled a drift detector after an adversarial test; see #7. |
| `t74` | **partial** | Split per `d6`. `t74a` delivered the six schemas byte-pinned, behavioural fixtures, and the provider-neutral `ToolProvider` harness at colleague SHA `28fee29`. Policy-composition fixtures deliberately **not** captured — blocked on `t69`/`t70`. |
| `t75` | delivered | AST source-segment drift gate over the six handlers; wired into CI's `inventory-gate` job where the clone exists. |
| `t76` | delivered | `Operation`, `OperationResult`, `Environment`, lifecycle pipeline, `HostRunner` skeleton, zero-dep guard, boundary check. |
| `t77` | delivered | `shell/evidence.py`, `docs/evidence-contract.md`. |
| `t78` | delivered | `shell/policy.py` — evaluator ported, no config-dir coupling, five source states distinguished. |
| `t79` | delivered | Gate inside `execute` on the post-rewrite operation; evidence wiring folded in (it was unowned by any plan task). |
| `t80` | delivered | Process groups, SIGTERM→SIGKILL escalation, per-platform orphan-prevention honesty. |
| `t81` | delivered | `fs.read`, `fs.list` — numbering-before-truncation preserved. |
| `t82` | delivered | `fs.write`, `fs.edit` — byte accounting pinned against fixtures; `check_write` wired (also unowned). |
| `t83` | delivered | `fs.media` — vendored slice only; absence of `flatten_parts`/`IMAGE_TOKEN_ESTIMATE` asserted. |
| `t84` | delivered | `process.exec`, `process.shell` — distinct profiles, separate capture, real effects/redaction integration. |
| `t85` | delivered | `shell policy check` / `policy explain`, `shell operation show`; exit 2 made reachable. |
| `t86` | **blocked** | colleague's PR. Definition of done requires *propose, don't push*; the migration proposal is not yet written. |
| `t87` | **blocked** | colleague's PR; depends on `t86`. |

**Added mid-run and delivered** (not in the confirmed plan):

| Task | Why it was added |
|---|---|
| `t74a` | The deliverable half of `t74` that the security lane provably does not affect (`d6`). |
| `t88` | Closes three integrity gaps found during the run, including an exploitable gate bypass found by independent review. |

**14 of 19 confirmed tasks delivered (3 of them in the earlier M0 run), 1 partial,
4 blocked. 2 tasks added and delivered.**

## Mid-work Decisions

Deviations `d5`–`d11` were recorded via `/deviate` during the run and are quoted
here rather than re-litigated.

- `d5` — t69/t70 filed as **private** GitHub security advisories, not public issues — colleague is a public repo released on PyPI at 1.51.0; a public issue describing a policy-guard bypass with no fix available is a zero-day disclosure, and t69's own acceptance criterion requires private-fix-then-disclose.
- `d6` — t74 split; `t74a` proceeds now at SHA `28fee29`, policy-composition fixtures hold — verified, not assumed: `colleague/tools.py` contains zero references to policy, and neither advisory's owned files include `tools.py`.
- `d7` — t76 runs parallel to `t74a` rather than sequentially behind it — the dependency is methodological, not content; separate worktrees preserve its intent.
- `d8` — version bump and CHANGELOG owned by the merging agent, not task agents — both files are shared by every task, so parallel agents bumping them would conflict on every merge.
- `d9` — fan-out slices land as **local merges**, not GitHub PRs (operator directive).
- `d10` — t78 proceeds without waiting for the advisories, excluding parent/child composition — settled from the spec: `c26` states the evaluator requires zero changes and only `load_policy` needs a seam; `c11` states location resolution does not move.
- `d11` — Milestone 1 closed with the remainder tracked in #8.

Decisions not covered by a deviation record:

- **The evidence-capture wiring was unowned by any plan task.** `t77` built
  `capture()`; nothing called it. Folded into `t79` rather than left as a gap —
  a denied operation leaving no evidence is the audit hole this package exists to
  close.
- **`check_write` was likewise unowned.** `t78` shipped it; nothing called it.
  Folded into `t82`.
- **Three existing tests were pinning defects and had to be rewritten, not
  updated** — one docstring rationalised the missing persistence block as
  intentional; another asserted `result.evidence.stdout == secret` and described
  the leak as a "known limit". Both now record that the earlier framing was wrong.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|---|---|---|
| `t69`, `t70` (`d5`) | Filed as private advisories rather than committed here — colleague-repo work, and public disclosure of an unfixed guard bypass was not acceptable | acceptable |
| `t74` (`d6`) | Split; policy-composition fixtures deliberately uncaptured so parity is never measured against pre-hardening composition | acceptable |
| `t76` (`d7`) | Ran parallel rather than behind `t74a` | acceptable |
| `t74`–`t85` (`d8`) | Version/CHANGELOG ownership moved to the merging agent | acceptable |
| all slices (`d9`) | Local merges rather than GitHub PRs, per operator directive | acceptable |
| `t78` (`d10`) | Proceeded without the colleague security fixes, composition excluded | acceptable |
| `t79` | Absorbed the unowned evidence-capture wiring beyond its stated scope | acceptable |
| `t82` | Absorbed the unowned `check_write` wiring beyond its stated scope | acceptable |
| `t86`, `t87` (`d11`) | Not attempted — colleague's PRs to make; the required migration proposal is unwritten | needs-follow-up |

## Evidence

- tests: `uv run pytest -n auto` — **638 passed, 1 skipped**. The skip is
  `tests/test_drift_gate.py:209`, colleague-checkout-dependent and expected.
- coverage: **96%** total; `shell/policy.py` 100%, `shell/operations.py` 99%,
  `shell/evidence.py` 94%.
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -r shell`
  (No issues identified), `markdownlint-cli2` (0 errors) — all clean.
- rubric: `uv run teken cli doctor . --strict` — 26 PASS, 0 FAIL.
- commits: `00e9897..803e29a` (39 commits, 14 slice merges) on
  `feat/milestone-1-operations-plane`.
- issues: #1 (source of truth), #7 (scanner keying, open), #8 (what M1 does not
  deliver).
- advisories: GHSA-6pfg-9vcp-hqw4, GHSA-2m42-prxw-43w6 (both draft/private).

## Delivery Claims

| Claim | Confidence | Evidence |
|---|---|---|
| The operation lifecycle exists and every operation passes through one gated pipeline | **high** | `shell/operations.py` 99% covered; `tests/test_dispatch_policy.py` |
| The policy gate judges the same *values* the handler runs | **high** | Exploit reproduced pre-fix (gate allowed `git status`, handler ran `rm -rf /`), re-run post-fix and closed; regression tests for top-level and nested cases |
| A declared secret does not reach the live result or the record | **high** | Re-ran the leak probe post-fix: `stdout == '[redacted]'`, absent from both bodies |
| `applied` never claims an operation ran when dispatch never reached the handler | **high** | Probed all seven terminal states; `crashed` reports `null`, not a fabricated boolean |
| Line numbering precedes truncation, matching colleague | **high** | `t81` reversed the implementation and observed three pins fail, then restored |
| `bytes_written` matches colleague | **high** | Pinned byte-for-byte against `tests/fixtures/colleague/behavior.json` |
| The six tool schemas are byte-equivalent to colleague at `28fee29` | **high** | `tests/fixtures/colleague/schemas.json`, generated not hand-written; regeneration proven reproducible |
| Base shell-cli remains pure-stdlib | **high** | Adversarially probed: injecting `import requests` fired three independent detectors |
| Host mode is documented as a guard, never a sandbox | **high** | `tests/test_honesty.py` scans all `shell/**/*.py`; `t84` wrote a file *outside* the work root to demonstrate rather than assert |
| The CLI and library use the same operation engine | **medium** | `t85` proved equivalence with a real `fs.read`; only `policy`/`operation` verbs exist, so the surface is narrow |
| colleague loses no behaviour in migration | **unverified** | No differential parity run — both implementations have never run in one session. That is `t87`, not done. |
| colleague has no unclassified direct subprocess path | **unverified** | Milestone 3. Debt counter stands at 13 modules. |
| Container mode's isolation profile is tested | **unverified** | No `ContainerRunner` exists. Milestone 4. |

## Remaining Work / Follow-up

Enumerated in full in **[#8](https://github.com/agentculture/shell-cli/issues/8)**.

- `t69`, `t70` — colleague must fix and release; disclosure follows. Advisories filed.
- `t74` (composition fixtures) — unblocks once those releases land.
- `t86`, `t87` — **write the migration proposal as an issue on colleague first.**
  This is the nearest actionable item and the definition of done in #1 requires it.
- **#7** — the inventory scanner is keyed per module, not per site; a new
  `shell=True` spawn in an already-allow-listed module passes green. Open,
  unscheduled, and it changes the pinned baseline.
- Milestones 3–5 — not started.
- **Before the first push to `main`:** every push publishes to PyPI, and 0.13.0
  contains the gate-bypass fix. Publishing it and disclosing the colleague
  advisories are separate decisions with an ordering between them. Operator's
  call; `docs/release-runbook.md` has the yank-and-fix-forward path.
