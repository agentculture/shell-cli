"""Tests for ``process.exec`` (``shell/process/exec.py``).

Every process test in this file runs a **real** process. That is deliberate: a
fake runner would pin the shape of the result and none of the behaviour the
result claims, and the claims here (separate capture, bounded output, honest
termination reporting, an incomplete effect list) are exactly the ones a mock
would let drift.

``shell.process.exec`` is imported at module scope on purpose. Handlers register
themselves on import, so a test asserting on ``handler_for("process.exec")``
that relied on some *other* test module having imported it first would pass
serially and fail under ``pytest -n auto``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from shell import operations
from shell.environment import Environment, WorkspaceKind
from shell.evidence import EvidenceRecord
from shell.operations import ExecutionProfile, Operation, OperationIntent
from shell.policy import load_policy
from shell.process import exec as process_exec
from shell.results import OperationResult, OperationStatus, PolicyDecision
from shell.runners.host import HostRunner
from shell.runners.types import ProcessOutcome

PYTHON = sys.executable


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    """Separate roots, so a test can tell control context from the work root."""
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        workspace=WorkspaceKind.WORKTREE,
        env_passthrough=("PATH",),
    )


def run(env: Environment, arguments: dict[str, Any], **kwargs: Any) -> OperationResult:
    kwargs.setdefault("apply", True)
    return operations.execute(Operation(kind=process_exec.KIND, arguments=arguments, **kwargs), env)


# --- registration -----------------------------------------------------------


def test_registers_as_an_execute_operation_defaulting_to_project() -> None:
    """The default profile is the LESS trusted of the two this kind accepts.

    Control-plane trust must be asked for in writing; a default that grants it
    to an unlabelled caller is the implicit decision that later reads as a
    deliberate one.
    """
    spec = operations.handler_for(process_exec.KIND)
    assert spec.intent is OperationIntent.EXECUTE
    assert spec.default_profile is ExecutionProfile.PROJECT
    assert process_exec.KIND in operations.registered_kinds()


# --- preview by default -----------------------------------------------------


def test_previews_without_apply_and_starts_nothing(env: Environment) -> None:
    marker = env.work_root / "ran.txt"
    result = run(
        env,
        {"argv": [PYTHON, "-c", f"open({str(marker)!r}, 'w').write('x')"]},
        apply=False,
    )
    assert result.status is OperationStatus.PREVIEWED
    assert not result.succeeded and not bool(result)
    assert not marker.exists(), "a preview must not start the process"


# --- real execution ---------------------------------------------------------


def test_captures_stdout_and_stderr_separately(env: Environment) -> None:
    """Acceptance 1: the neutral result never merges the two streams.

    The first consumer renders them concatenated; that concatenation is the
    adapter's job, because a record that merged them cannot recover which line
    came from where.
    """
    result = run(
        env,
        {
            "argv": [
                PYTHON,
                "-c",
                "import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR')",
            ]
        },
    )
    assert result.status is OperationStatus.SUCCEEDED
    assert result.evidence.stdout == "OUT"
    assert result.evidence.stderr == "ERR"
    assert "ERR" not in result.evidence.stdout
    assert "OUT" not in result.evidence.stderr
    assert result.evidence.stdout_bytes == 3
    assert result.evidence.stderr_bytes == 3


def test_argv_is_not_re_split_by_a_shell(env: Environment) -> None:
    """An argument containing shell metacharacters arrives verbatim."""
    hostile = "a b; echo pwned > owned.txt && $(whoami)"
    result = run(
        env, {"argv": [PYTHON, "-c", "import sys; sys.stdout.write(sys.argv[1])", hostile]}
    )
    assert result.evidence.stdout == hostile
    assert not (env.work_root / "owned.txt").exists()


def test_a_non_zero_exit_is_a_succeeded_operation(env: Environment) -> None:
    """The operation is "run and observe"; the exit code is the observation.

    Matches the first consumer, whose ``run_command`` returns a successful tool
    call for ``exit=3`` (``tests/fixtures/colleague/behavior.json``).
    """
    result = run(env, {"argv": [PYTHON, "-c", "raise SystemExit(3)"]})
    assert result.status is OperationStatus.SUCCEEDED
    assert result.output["exit_code"] == 3
    assert result.error == ""


def test_a_process_that_cannot_start_is_failed_with_no_exit_code(env: Environment) -> None:
    result = run(env, {"argv": [str(env.work_root / "definitely-not-a-program")]})
    assert result.status is OperationStatus.FAILED
    assert result.output["exit_code"] is None
    assert "could not start the process" in result.error


def test_cwd_is_the_work_root(env: Environment) -> None:
    result = run(env, {"argv": [PYTHON, "-c", "import os,sys; sys.stdout.write(os.getcwd())"]})
    assert Path(result.evidence.stdout).resolve() == env.work_root
    assert Path(result.output["cwd"]).resolve() == env.work_root


def test_the_environment_is_an_allow_list_not_a_passthrough(tmp_path: Path) -> None:
    """An unlisted variable does not reach the child, and nothing restores it.

    A handler quietly adding ``PATH`` or ``HOME`` back would make the caller's
    declared policy a fiction, so this pins that it does not.
    """
    os.environ["SHELL_CLI_TEST_MARKER"] = "leaked"
    try:
        environment = Environment(
            source_root=tmp_path,
            work_root=tmp_path,
            runner=HostRunner(),
            env_passthrough=("SHELL_CLI_TEST_MARKER",),
        )
        result = run(
            environment,
            {"argv": [PYTHON, "-c", "import os,sys; sys.stdout.write(repr(sorted(os.environ)))"]},
        )
        assert result.status is OperationStatus.SUCCEEDED
        seen = result.evidence.stdout
        assert "SHELL_CLI_TEST_MARKER" in seen
        assert "'HOME'" not in seen
        assert result.output["env_names"] == ["SHELL_CLI_TEST_MARKER"]
    finally:
        del os.environ["SHELL_CLI_TEST_MARKER"]


def test_output_is_bounded_and_the_bound_is_reported(env: Environment) -> None:
    result = run(
        env,
        {"argv": [PYTHON, "-c", "import sys; sys.stdout.write('a' * 5000)"]},
        max_output_bytes=100,
    )
    assert result.evidence.stdout == "a" * 100
    assert result.evidence.stdout_truncated is True
    # The count is of the ORIGINAL stream, so a truncation reports how much was
    # dropped rather than only that something was.
    assert result.evidence.stdout_bytes == 5000
    assert result.output["max_output_bytes"] == 100


def test_a_timeout_is_its_own_status_and_records_the_escalation(env: Environment) -> None:
    result = run(env, {"argv": [PYTHON, "-c", "import time; time.sleep(30)"]}, timeout_seconds=0.3)
    assert result.status is OperationStatus.TIMED_OUT
    assert result.status is not OperationStatus.FAILED
    termination = result.output["termination"]
    assert termination["reason"] == "timeout"
    assert termination["steps"], "the escalation path must be recorded, not just the fact"
    assert termination["orphan_prevention"] in {"process-group", "direct-child-only"}
    assert termination["orphan_prevention_note"]


# --- profiles ---------------------------------------------------------------


def test_a_control_profile_is_accepted_and_recorded(env: Environment) -> None:
    result = run(env, {"argv": [PYTHON, "-c", "pass"]}, profile=ExecutionProfile.CONTROL)
    assert result.status is OperationStatus.SUCCEEDED
    assert result.output["profile"] == "control"


def test_the_observe_profile_is_refused(env: Environment) -> None:
    """``observe`` is for structured reads that spawn nothing."""
    result = run(env, {"argv": [PYTHON, "-c", "pass"]}, profile=ExecutionProfile.OBSERVE)
    assert result.status is OperationStatus.FAILED
    assert "'observe' profile" in result.error


def test_the_declared_profile_reaches_the_evidence_record(env: Environment) -> None:
    records: list[EvidenceRecord] = []
    operations.execute(
        Operation(
            kind=process_exec.KIND,
            arguments={"argv": [PYTHON, "-c", "pass"]},
            profile=ExecutionProfile.CONTROL,
            apply=True,
            caller={"agent": "colleague", "tool": "hook"},
        ),
        env,
        evidence_sink=records.append,
    )
    body = records[0].body
    assert body["operation"]["normalized"]["profile"] == "control"
    assert body["operation"]["kind"] == "process.exec"
    assert body["caller"]["tool"] == "hook"


# --- argument errors are recoverable steps ----------------------------------


def test_a_raw_shell_string_is_refused_by_name(env: Environment) -> None:
    """Acceptance 5: control operations use argv vectors, never raw strings.

    Offering ``command`` to ``process.exec`` is a category error and is reported
    as one rather than silently ignored — a silently ignored key would run
    nothing and report success for an operation the caller believed it had
    described.
    """
    result = run(env, {"command": "echo hi"})
    assert result.status is OperationStatus.FAILED
    assert "never a raw shell string" in result.error
    assert "process.shell" in result.error


@pytest.mark.parametrize("arguments", [{}, {"argv": []}, {"argv": "echo hi"}])
def test_a_malformed_argv_is_a_failed_result_not_an_exception(
    env: Environment, arguments: dict[str, Any]
) -> None:
    result = run(env, arguments)
    assert result.status is OperationStatus.FAILED
    assert "requires 'argv'" in result.error


# --- effects and policy -----------------------------------------------------


def test_effects_are_never_claimed_complete(env: Environment) -> None:
    result = run(env, {"argv": [PYTHON, "-c", "pass"]})
    assert result.effects_complete is False
    assert result.effects.changed_paths == ()


def test_the_run_command_policy_has_jurisdiction_over_process_exec(env: Environment) -> None:
    """``process.*`` is gated, unlike the deliberately carved-out ``fs.*``.

    The gate is inside ``execute``; this pins that the handler inherits it
    rather than re-implementing one of its own.
    """
    policy = load_policy(data={"run_command": {"allow": [], "deny": [PYTHON]}})
    result = operations.execute(
        Operation(kind=process_exec.KIND, arguments={"argv": [PYTHON, "-c", "pass"]}, apply=True),
        env,
        policy=policy,
    )
    assert result.status is OperationStatus.DENIED
    assert result.verdict.decision is PolicyDecision.DENIED


# --- the outcome mapping, for states the pipeline cannot reach yet ----------
#
# ``run_process`` accepts a cancel event and can report a process it failed to
# reap, but ``execute`` has no cancellation channel, so neither state arrives
# through a real run today. They are states of the runner's own outcome type
# rather than speculative ones, so the mapping is written — and exercised here
# directly, because a mapping that has never run once is a guess.


def test_a_cancelled_outcome_is_failed_and_says_so() -> None:
    outcome = ProcessOutcome(exit_code=None, cancelled=True)
    status, error = process_exec._status(process_exec.KIND, outcome)
    assert status is OperationStatus.FAILED
    assert "cancelled" in error
    assert "(cancelled)" in process_exec.render(process_exec.KIND, outcome, 1000)


def test_an_unreaped_process_is_failed_rather_than_a_silent_success() -> None:
    """``exit_code is None`` with no start error means termination never reaped it.

    Falling through to SUCCEEDED here would report a process nobody watched exit
    as a clean run.
    """
    outcome = ProcessOutcome(exit_code=None)
    status, error = process_exec._status(process_exec.KIND, outcome)
    assert status is OperationStatus.FAILED
    assert "not reaped" in error
