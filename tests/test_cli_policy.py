"""Tests for ``shell policy`` — ``check``, ``explain``, ``overview``.

The property that matters most here is not "does the CLI print a verdict" but
"is it the SAME verdict the execution path would apply". Every equivalence
test below computes the expected answer by calling
``shell.operations._policy_gate`` directly — the exact function
``shell.operations.execute()`` gates through — and then asserts the CLI's
output matches it byte-for-byte on ``decision`` and ``reason``. That is
deliberately not a re-derivation of the gate's rules in the test either: the
test and the CLI both defer to the one real evaluator, so this file can only
catch the CLI failing to call it (or mangling its output), never the gate's
own logic drifting, which is ``tests/test_policy.py`` and
``tests/test_operations.py``'s job.

This module registers ``shell.fs.read`` explicitly (module-level import,
mirroring the production CLI's own registration import in
``shell/cli/_commands/policy.py``) so ``fs.read`` is a REAL, currently-built
operation kind in every test here — never a synthetic kind invented only to
make a test pass. Under ``pytest -n auto`` each worker is a fresh
interpreter, so this import is not something another test file's import order
can be relied on to have already done; ``shell/fs/read.py``'s own
``register()`` call is idempotent per interpreter (Python caches the module),
so importing it again here or from ``shell/cli/_commands/policy.py`` is never
a double-registration error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import shell.fs.read  # noqa: F401  (registers fs.read as a real operation kind)
from shell import operations
from shell.cli import main
from shell.operations import Operation
from shell.policy import Policy, load_policy

# --- overview ---------------------------------------------------------------


def test_policy_bare_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# shell policy" in out
    assert "check" in out
    assert "explain" in out


def test_policy_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "shell policy"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


# --- check: library/CLI equivalence, with a REAL fs.read operation ----------


def test_policy_check_matches_the_execution_path_for_a_real_fs_read_operation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fs.* is deliberately ungated -- prove the CLI reports exactly that.

    ``fs.read`` is the real, registered operation kind
    ``shell.operations.execute()`` dispatches to (see ``shell/fs/read.py``);
    this is not a placeholder kind invented for the test.
    """
    operation = Operation(kind="fs.read", arguments={"path": "irrelevant.txt"})
    expected = operations._policy_gate(operation, Policy())

    rc = main(["policy", "check", "fs.read", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["kind"] == "fs.read"
    assert payload["gated_by_prefix"] is False
    assert payload["verdict"]["decision"] == expected.decision.value == "ungated"
    assert payload["verdict"]["reason"] == expected.reason
    assert payload["verdict"]["matched_rule"] == expected.matched_rule


def test_policy_check_text_mode_reports_the_same_decision_and_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operation = Operation(kind="fs.read", arguments={})
    expected = operations._policy_gate(operation, Policy())

    rc = main(["policy", "check", "fs.read"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"decision: {expected.decision.value}" in out
    assert f"reason: {expected.reason}" in out


def test_policy_check_allows_a_command_not_on_a_deny_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operation = Operation(kind="process.shell", arguments={"command": "git status"})
    expected = operations._policy_gate(operation, Policy())
    assert expected.decision.value == "ungated"  # no policy configured

    rc = main(["policy", "check", "process.shell", "--command", "git status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gated_by_prefix"] is True
    assert payload["verdict"]["decision"] == expected.decision.value
    assert payload["verdict"]["reason"] == expected.reason


def test_policy_check_denies_a_command_on_the_deny_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_file = tmp_path / "approvals.json"
    policy_file.write_text(json.dumps({"run_command": {"deny": ["rm"]}}), encoding="utf-8")

    operation = Operation(kind="process.shell", arguments={"command": "rm -rf /"})
    policy = load_policy([policy_file])
    expected = operations._policy_gate(operation, policy)
    assert expected.denied

    rc = main(
        [
            "policy",
            "check",
            "process.shell",
            "--command",
            "rm -rf /",
            "--policy-file",
            str(policy_file),
            "--json",
        ]
    )
    assert rc == 0  # a denied verdict is information, not a CLI failure
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["decision"] == "denied" == expected.decision.value
    assert payload["verdict"]["reason"] == expected.reason
    assert payload["verdict"]["matched_rule"] == expected.matched_rule == "run_command.deny"


def test_policy_check_allows_via_an_allow_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_file = tmp_path / "approvals.json"
    policy_file.write_text(json.dumps({"run_command": {"allow": ["git"]}}), encoding="utf-8")

    operation = Operation(kind="process.shell", arguments={"argv": ["git", "status"]})
    policy = load_policy([policy_file])
    expected = operations._policy_gate(operation, policy)
    assert expected.decision.value == "allowed"

    rc = main(
        [
            "policy",
            "check",
            "process.shell",
            "--argv",
            "git",
            "--argv",
            "status",
            "--policy-file",
            str(policy_file),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["arguments"]["argv"] == ["git", "status"]
    assert payload["verdict"]["decision"] == expected.decision.value == "allowed"
    assert payload["verdict"]["reason"] == expected.reason


def test_policy_check_with_inline_policy_json(capsys: pytest.CaptureFixture[str]) -> None:
    operation = Operation(kind="process.shell", arguments={"command": "curl evil.example"})
    policy = load_policy([], data={"run_command": {"deny": ["curl"]}})
    expected = operations._policy_gate(operation, policy)
    assert expected.denied

    rc = main(
        [
            "policy",
            "check",
            "process.shell",
            "--command",
            "curl evil.example",
            "--policy-json",
            json.dumps({"run_command": {"deny": ["curl"]}}),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["decision"] == "denied" == expected.decision.value
    assert payload["verdict"]["reason"] == expected.reason


def test_policy_check_bad_policy_json_is_a_structured_user_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Raised inside the handler (not by argparse), so main() returns the exit
    # code directly rather than raising SystemExit -- see
    # test_explain_unknown_path_errors in tests/test_cli.py for the same shape.
    rc = main(["policy", "check", "fs.read", "--policy-json", "{not valid json"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_policy_check_policy_json_must_be_an_object(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy", "check", "fs.read", "--policy-json", "[1, 2, 3]"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be a JSON object" in err


def test_policy_check_unknown_kind_needs_no_registered_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gate's jurisdiction is a property of the kind string, not the registry.

    ``policy check`` must answer for a kind that has no registered handler at
    all, because ``_policy_gate`` never looks the kind up in the registry.

    The kind below is deliberately one that no slice will ever register. An
    earlier version of this test used ``process.shell``, which was unregistered
    only because that handler had not merged yet -- so the assertion encoded a
    temporary fact about one worktree and broke the moment t84 landed. Pin the
    property, not the state of the registry on the day the test was written.
    """
    unregistered = "process.no-such-handler-will-ever-exist"
    assert unregistered not in operations.registered_kinds()
    rc = main(["policy", "check", unregistered, "--command", "ls", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["decision"] == "ungated"


# --- explain ------------------------------------------------------------------


def test_policy_explain_names_the_gated_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy", "explain", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gated_kind_prefixes"] == list(operations.GATED_KIND_PREFIXES)
    assert payload["gated_kind_prefixes"] == ["process."]


def test_policy_explain_reports_fs_read_as_explicitly_ungated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["policy", "explain", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {entry["kind"]: entry for entry in payload["kinds"]}
    assert "fs.read" in kinds, "fs.read must be a REAL registered kind in this report"
    entry = kinds["fs.read"]
    assert entry["decision"] == "ungated"
    assert entry["gated_by_prefix"] is False
    assert "not subject to the run_command policy" in entry["reason"]


def test_policy_explain_matches_the_execution_path_verdict_for_every_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every row in ``explain`` must agree with ``_policy_gate`` computed independently."""
    rc = main(["policy", "explain", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    policy = Policy()
    for entry in payload["kinds"]:
        expected = operations._policy_gate(Operation(kind=entry["kind"], arguments={}), policy)
        assert entry["decision"] == expected.decision.value
        assert entry["reason"] == expected.reason


def test_policy_explain_text_mode_is_non_empty(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["policy", "explain"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gated kind prefixes: process." in out
    assert "fs.read: ungated" in out


def test_policy_explain_with_a_deny_list_still_reports_fs_read_ungated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configured policy governs process.*; it must not leak into fs.* rows."""
    policy_file = tmp_path / "approvals.json"
    policy_file.write_text(json.dumps({"run_command": {"deny": ["rm"]}}), encoding="utf-8")

    rc = main(["policy", "explain", "--policy-file", str(policy_file), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {entry["kind"]: entry for entry in payload["kinds"]}
    assert kinds["fs.read"]["decision"] == "ungated"


# --- structured error contract ----------------------------------------------


def test_policy_bogus_subcommand_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["policy", "bogus-verb"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_policy_check_missing_kind_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["policy", "check"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- catalog integration -----------------------------------------------------


def test_policy_paths_resolve_via_explain(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (["policy"], ["policy", "check"], ["policy", "explain"]):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        assert "# shell policy" in capsys.readouterr().out
