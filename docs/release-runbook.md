# Release runbook

How a shell-cli version reaches PyPI, what gates it must clear first, and what
to do when a bad one gets out.

This exists because **PyPI is append-only**. A published version can never be
replaced or edited — only yanked, which hides it from new resolution while
leaving it installable by exact pin. Every release is therefore permanent, and
colleague is about to depend on these releases. The cost of a bad publish is
paid by a downstream consumer, not by us.

## How a release happens

There is no manual release step. **Every push to `main` publishes**, because
every PR bumps `pyproject.toml` (enforced by the `version-check` job in
`.github/workflows/tests.yml`) and `pyproject.toml` is in the `paths:` filter of
`.github/workflows/publish.yml`. There is no such thing as a docs-only merge
that does not release.

Two consequences worth internalising:

- **A merge is a release.** Treat "should this merge?" and "should this ship to
  every consumer?" as the same question.
- **Version numbers are consumed immediately.** A bumped-but-reverted PR still
  burns its version if it merged; pick the next one and move forward.

## The gate chain

`publish` runs only after **all three** of these jobs pass:

| Job | What it proves |
|---|---|
| `test` | `pytest -n auto` — the full suite, including `tests/test_honesty.py` |
| `gates` | `black`, `isort`, `flake8`, `bandit`, `markdownlint-cli2`, `teken cli doctor --strict` |
| `smoke` | The built wheel installs into a clean venv, imports, declares zero runtime dependencies, and its `shell` console script runs |

Before this runbook existed, `publish` needed only `test`. Lint, security
scanning, the markdown gate and the agent-first rubric gate all lived solely in
`tests.yml`, so any of them could be red while a release went out.

`smoke` is the job that covers the gap the test suite structurally cannot:
`uv sync` installs from the source tree, so a green suite says nothing about
whether the **built artifact** imports, whether its entry point is wired up, or
what its metadata declares. In particular it asserts the published wheel
declares **no runtime dependencies** — the pure-stdlib constraint, checked
against the artifact rather than the checkout.

The `gates` job duplicates the `lint` job in `tests.yml` on purpose. A job in
one workflow file cannot appear in another's `needs:`, and the alternative —
letting `publish` trust that a separate workflow happened to pass — is exactly
the hole being closed. **If you change a gate in one file, change it in the
other.**

## Yank and fix forward

Yanking is the only lever PyPI gives us, and it is weaker than it sounds:

- a yanked version **stays installable** by exact pin (`shell-cli==0.8.1`);
- it is skipped by range resolution (`>=0.8,<0.9`);
- anything already installed or already locked is unaffected.

So yanking limits the blast radius for *future* installs. It does not recall
anything. The fix is always to **ship a good version**, and the yank is only
there to stop new consumers landing on the bad one.

### Procedure

1. **Confirm the artifact is actually broken**, not the consumer's environment.
   Reproduce against the published wheel in a clean venv, the same way `smoke`
   does:

   ```bash
   python3 -m venv /tmp/verify
   /tmp/verify/bin/python -m pip install shell-cli==<bad-version>
   /tmp/verify/bin/python -c "import shell"
   /tmp/verify/bin/shell whoami --json
   ```

2. **Yank it**, on the PyPI project page under *Manage → Releases*, with a
   reason a stranger can act on ("`shell` console script missing from wheel
   metadata; use 0.8.2"). Trusted publishing does not grant yank rights, so
   this step is a human with project-owner access — it cannot be automated
   away.

3. **Tell the consumers before they discover it.** At minimum, comment on the
   colleague issue tracking the dependency. A yank is silent to anyone already
   pinned.

4. **Fix forward on a normal PR.** Bump to the next patch version — never reuse
   the yanked number, and never try to re-upload it; PyPI will refuse.

5. **Close the hole.** A bad release that cleared all three gates means the
   gates missed something. Add the check that would have caught it *in the same
   PR as the fix*, so the runbook's gate chain grows a row.

### If the bad version was already pinned by colleague

Yanking does not help — an exact pin still resolves. Ship the fixed version,
then open a PR on colleague moving the pin. That PR is colleague's to merge;
see the boundary note below.

## How colleague depends on shell-cli

colleague pins shell-cli with an **upper bound until 1.0**:

```toml
dependencies = ["agentfront>=0.20.0", "shell-cli>=0.8,<0.9"]
```

Pre-1.0 semver gives no compatibility promise across minor versions, and
shell-cli's operation/result contracts are still moving. An unbounded pin would
let a breaking 0.9 reach colleague through an unrelated `uv sync`. The bound is
raised deliberately, in a colleague PR that runs colleague's own suite — never
implicitly.

**colleague is the second sanctioned base dependency**, alongside `agentfront`.
Its zero-dependency guard allow-lists exactly one name today. The colleague PR
that adds the shell-cli dependency **must update
`test_base_dependency_is_exactly_agentfront` and its docstring in the same
commit** — the test name and its prose both encode "exactly one", and a change
that updates the assertion while leaving the docstring claiming otherwise turns
the guard into a lie that reads as intentional.

### The boundary

shell-cli does not push changes into colleague. Every colleague-side change
described here — adding the dependency, updating the guard test, moving the
pin — is **proposed as a PR or issue and merged by colleague**. shell-cli
imports nothing from colleague; colleague imports shell-cli.

**One exception: the security fix is exempt from the migration-proposal gate.**
The `run_command` policy fix does not wait on the shell-cli migration proposal,
does not wait on a shell-cli release, and is not bundled with migration work. It
ships on colleague's own schedule as a private security advisory, and the
migration sequencing is downstream of it — not the other way round. Holding a
live policy-bypass fix behind an extraction proposal would be trading a real
vulnerability window for tidy sequencing.

## Before you merge

- [ ] Version bumped (`/version-bump patch|minor|major`) — CI blocks without it
- [ ] `CHANGELOG.md` entry describes the change in consumer-facing terms
- [ ] All three gate jobs green on the PR
- [ ] The TestPyPI dev release installed and smoke-tested, if the change touches
      packaging, entry points, or `pyproject.toml` metadata
- [ ] You would be comfortable with every consumer picking this up automatically
