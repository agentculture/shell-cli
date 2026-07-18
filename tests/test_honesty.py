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
_OVERCLAIM = re.compile(
    r"\b(is|as|provides?|offers?|ships?|implements?|guarantees?)\s+(a\s+|an\s+)?"
    r"(secure\s+|safe\s+|real\s+)?sandbox"
    r"|\bsandboxe[sd]\b"
    r"|\bfully\s+isolated\b",
    re.IGNORECASE,
)

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
    hits = _OVERCLAIM.findall(text)
    assert not hits, f"{name} appears to claim isolation shell-cli does not provide: {hits}"


def test_catalog_makes_no_isolation_claim() -> None:
    offenders = {name: _OVERCLAIM.findall(body) for name, body in _catalog_texts().items()}
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
        assert not _OVERCLAIM.findall(text), f"{label} overclaims isolation"
