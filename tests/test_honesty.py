"""Guard tests for the guard-not-a-sandbox honesty constraint.

The mission (issue #1) commits shell-cli to an explicit posture: the execution
gate is best-effort and bypassable, so nothing in the shipped surface may imply
isolation the code does not provide. A package *named* shell-cli whose headline
is safe execution will be read as offering a sandbox — these tests make the
disclaimer load-bearing rather than a docstring someone can quietly drop.

Two directions are guarded:

1. The disclaimer is PRESENT in the surfaces an agent actually reads
   (``learn``, ``explain`` root, ``explain safety``, README, threat model).
2. No surface makes a POSITIVE isolation claim — the word "sandbox" may only
   ever appear in a negating context.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shell.cli import main
from shell.explain.catalog import ENTRIES

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Phrasings that would claim isolation this package does not implement. Kept
# deliberately narrow: the goal is to catch an affirmative claim, not to ban the
# word (the honest disclaimer must be free to say "not a sandbox").
#
# Non-capturing groups throughout so ``finditer`` -> ``group(0)`` reports the
# offending text itself; with capturing groups a failure would print tuples of
# fragments (``[('', '', '')]``) and tell the reader nothing.
_CLAIM = re.compile(
    r"\b(?:is|as|provides?|offers?|ships?|implements?|guarantees?)\s+(?:an?\s+)?"
    r"(?:secure\s+|safe\s+|real\s+)?sandbox\b"
    r"|\bsandboxe[sd]\b"
    r"|\bfully\s+isolated\b",
    re.IGNORECASE,
)

# Words that turn one of the above into a disclaimer rather than a claim. This
# guard must never fire on honest text — "it is never sandboxed" is the posture
# stated *more* strongly, and failing CI over it would punish exactly the writing
# this file exists to protect.
_NEGATOR = re.compile(
    r"\b(?:not|never|n't|no|nor|neither|without|rather\s+than|instead\s+of)\b",
    re.IGNORECASE,
)


def overclaims(text: str) -> list[str]:
    """Return affirmative isolation claims in *text*, as the matched substrings.

    A match is discounted when a negator appears earlier **in the same
    sentence**. Negation is checked over that window rather than as a regex
    lookbehind because Python requires fixed-width lookbehind and the negator
    can sit several words back ("is never actually sandboxed"). Stopping at the
    sentence boundary keeps a disclaimer in one sentence from excusing a genuine
    claim in the next.
    """
    found = []
    for match in _CLAIM.finditer(text):
        start = max(
            text.rfind(".", 0, match.start()),
            text.rfind("!", 0, match.start()),
            text.rfind("?", 0, match.start()),
            text.rfind("\n", 0, match.start()),
        )
        if _NEGATOR.search(text[start + 1 : match.start()]):
            continue
        found.append(match.group(0))
    return found


_DOC_FILES = ["README.md", "CLAUDE.md", "docs/threat-model.md"]


def _catalog_texts() -> dict[str, str]:
    return {" ".join(path) or "<root>": body for path, body in ENTRIES.items()}


# --- 1. the disclaimer is present -----------------------------------------


def test_explain_safety_entry_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "safety"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not a sandbox" in out.lower()
    assert "does not protect against" in out.lower()


def test_explain_root_carries_the_posture(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert "not a sandbox" in capsys.readouterr().out.lower()


def test_learn_carries_the_posture(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    assert "not a sandbox" in capsys.readouterr().out.lower()


def test_learn_json_exposes_safety_posture(capsys: pytest.CaptureFixture[str]) -> None:
    """Machine consumers get the posture as a field, not buried in prose."""
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "not a sandbox" in payload["safety_posture"].lower()


@pytest.mark.parametrize("name", _DOC_FILES)
def test_doc_carries_the_posture(name: str) -> None:
    text = (_REPO_ROOT / name).read_text(encoding="utf-8").lower()
    assert "not a sandbox" in text, f"{name} must state the guard-not-a-sandbox posture"


# --- 2. nothing overclaims -------------------------------------------------


@pytest.mark.parametrize("name", _DOC_FILES)
def test_doc_makes_no_isolation_claim(name: str) -> None:
    text = (_REPO_ROOT / name).read_text(encoding="utf-8")
    hits = overclaims(text)
    assert not hits, f"{name} appears to claim isolation shell-cli does not provide: {hits}"


def test_catalog_makes_no_isolation_claim() -> None:
    offenders = {name: overclaims(body) for name, body in _catalog_texts().items()}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"explain catalog overclaims isolation: {offenders}"


def test_cli_help_and_learn_make_no_isolation_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["learn"])
    learn_text = capsys.readouterr().out
    main([])
    help_text = capsys.readouterr().out
    for label, text in (("learn", learn_text), ("help", help_text)):
        hits = overclaims(text)
        assert not hits, f"{label} overclaims isolation: {hits}"


# --- 3. the guard itself is trustworthy ------------------------------------
#
# A guard that cries wolf gets deleted, and one that never fires is decoration.
# Both directions are pinned here so a future tweak to the pattern cannot
# quietly break either.


@pytest.mark.parametrize(
    "honest",
    [
        "This is not fully isolated.",
        "Do not treat this as a sandbox.",
        "It is never sandboxed.",
        "a guard, not a sandbox",
        "It protects against careless behaviour, not an adversarial one.",
        "There is no namespace, container, or seccomp isolation.",
        "This is a guard rather than a sandbox.",
        "The gate runs without a sandbox.",
    ],
)
def test_honest_phrasing_does_not_trip_the_guard(honest: str) -> None:
    assert overclaims(honest) == [], f"guard fired on honest text: {honest!r}"


@pytest.mark.parametrize(
    "claim",
    [
        "shell-cli provides a secure sandbox.",
        "Every command is sandboxed.",
        "The runner offers a sandbox for untrusted code.",
        "Commands are fully isolated.",
        "It guarantees a sandbox.",
    ],
)
def test_affirmative_claim_trips_the_guard(claim: str) -> None:
    assert overclaims(claim), f"guard missed an overclaim: {claim!r}"


def test_disclaimer_does_not_excuse_a_later_claim() -> None:
    """A negator must not leak across a sentence boundary."""
    text = "This is not a sandbox. Every command is sandboxed anyway."
    assert overclaims(text) == ["sandboxed"]


def test_failure_message_reports_the_offending_text() -> None:
    """Regression: capturing groups made findall report ``[('', '', '')]``."""
    hits = overclaims("Commands are fully isolated.")
    assert hits == ["fully isolated"]


# --- 4. unbuilt capability is not stated as a present-tense guarantee -------
#
# The ``_CLAIM`` regex above catches the word "sandbox" and the phrase "fully
# isolated". It cannot catch a *table* that asserts "Execution isolation" for a
# Container runner that does not exist — which is exactly what shipped in the
# README's environment matrix and was caught by an external reviewer, not by
# this file.
#
# The design-target table is legitimate: the two-axis model is the spec, and
# writing it down is the point. What is not legitimate is presenting it so a
# reader takes it for shipped behaviour. So the guard is on the *framing*, not
# on the words: every row must declare whether it is built.


def _environment_table_section() -> str:
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    _, _, rest = text.partition("### Environments have two independent axes")
    assert rest, "README lost the environment-axes section"
    section, _, _ = rest.partition("\n### ")
    return section


def test_environment_table_is_framed_as_a_design_target() -> None:
    section = _environment_table_section().lower()
    assert "design target" in section, (
        "the environment matrix must say it is a design target — otherwise a "
        "reader takes the Guarantee column for shipped behaviour"
    )


def _environment_table_rows() -> list[list[str]]:
    """Every data row of the environment table, as stripped cells.

    Selected structurally — the pipe-table's header separator marks where data
    starts — rather than by matching on cell *content*. An earlier version
    filtered rows containing "Host" or "Container", which silently matched
    nothing (and so asserted nothing) the moment a runner was renamed. A guard
    that quietly stops guarding is worse than no guard, so the shape of the
    table decides what counts as a row, not the words in it.
    """
    lines = [ln.strip() for ln in _environment_table_section().splitlines()]
    table = [ln for ln in lines if ln.startswith("|")]
    separators = [i for i, ln in enumerate(table) if set(ln) <= set("|-: ")]
    assert separators, "environment table lost its header separator"
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in table[separators[0] + 1 :]]


def test_every_environment_row_declares_whether_it_is_built() -> None:
    """A runner row may describe intent, but never silently imply it exists."""
    rows = _environment_table_rows()
    assert rows, "expected data rows in the environment table"
    for cells in rows:
        assert cells[-1] in {
            "No",
            "Yes",
        }, f"environment row must end in a Built? cell of No/Yes, got {cells[-1]!r}: {cells}"


# --- 5. the library surface, not just the docs -----------------------------
#
# The doc checks above cover what a *human* reads. A consumer of the library
# reads docstrings and result payloads, and those are where an overclaim would
# do the most damage — a runner docstring asserting isolation is read as the
# contract by the very code that depends on it. So the source of the package is
# scanned with the same guard, and the runner's posture is required to survive
# all the way onto a result, not merely to exist in a docstring.


def _package_sources() -> list[Path]:
    return sorted((_REPO_ROOT / "shell").rglob("*.py"))


def test_package_source_makes_no_isolation_claim() -> None:
    """No docstring, comment or string literal in shell/ claims isolation."""
    offenders: dict[str, list[str]] = {}
    for path in _package_sources():
        hits = overclaims(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(_REPO_ROOT))] = hits
    assert not offenders, f"shell/ source overclaims isolation: {offenders}"


def test_the_host_runner_states_the_posture_in_its_own_description() -> None:
    from shell.runners.host import HostRunner

    described = HostRunner().describe()
    assert described["isolation"] == "none"
    assert "not a sandbox" in described["isolation_note"].lower()
    assert overclaims(described["isolation_note"]) == []


def test_the_posture_reaches_result_metadata(tmp_path: Path) -> None:
    """Documentation *and result metadata* must say so plainly — so check both.

    A consumer that never reads the README still receives the posture, because
    every result the pipeline returns carries the runner's own self-description.
    """
    from shell import operations
    from shell.environment import Environment
    from shell.operations import ExecutionProfile, Operation, OperationIntent
    from shell.results import OperationResult, OperationStatus
    from shell.runners.host import HostRunner

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        return OperationResult(operation_id=operation.id, status=OperationStatus.SUCCEEDED)

    kind = "test.honesty-posture"
    operations.register(
        kind,
        intent=OperationIntent.OBSERVE,
        default_profile=ExecutionProfile.OBSERVE,
        run=_run,
    )
    try:
        environment = Environment(source_root=tmp_path, work_root=tmp_path, runner=HostRunner())
        result = operations.execute(Operation(kind=kind), environment)
    finally:
        operations.unregister(kind)

    payload = result.to_dict()["evidence"]
    assert payload["isolation"] == "none"
    assert "not a sandbox" in payload["isolation_note"].lower()
    assert overclaims(payload["isolation_note"]) == []
