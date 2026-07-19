"""Tests for the operation core: Operation, OperationResult, Environment, pipeline.

The properties pinned here are the ones a consumer's safety depends on, not the
shape of the dataclasses:

* a preview is never a success, by any predicate the package exposes;
* the policy gate runs before the preview branch;
* a caller cannot relabel a mutation as an observation to skip that branch;
* a handler crash is a recoverable failed result, never an exception;
* every result carries the runner's honest isolation posture in its evidence;
* the workspace axis and the runner axis vary independently.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from shell import operations
from shell.environment import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    Environment,
    NetworkPolicy,
    WorkspaceKind,
)
from shell.operations import ExecutionProfile, Operation, OperationIntent
from shell.results import (
    SCHEMA_VERSION,
    Effects,
    Evidence,
    OperationResult,
    OperationStatus,
    PolicyDecision,
    PolicyVerdict,
)
from shell.runners import Runner
from shell.runners.host import HostRunner


class _FakeContainerRunner:
    """Stands in for a runner that *does* enforce a boundary.

    Exists only so the runner axis can be varied in a test without waiting for
    the real container runner. It runs nothing.
    """

    name = "fake-container"
    isolation = "declared"
    isolation_note = "test double; carries out no work"

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "isolation": self.isolation}


@pytest.fixture
def registry() -> Iterator[Callable[..., str]]:
    """Register throwaway operation kinds and clean them up afterwards."""
    registered: list[str] = []

    def _register(
        kind: str,
        *,
        intent: OperationIntent,
        run: Callable[[Operation, Environment], OperationResult],
        profile: ExecutionProfile = ExecutionProfile.OBSERVE,
    ) -> str:
        operations.register(kind, intent=intent, default_profile=profile, run=run)
        registered.append(kind)
        return kind

    yield _register

    for kind in registered:
        operations.unregister(kind)


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    source = tmp_path / "checkout"
    work = tmp_path / "worktree"
    source.mkdir()
    work.mkdir()
    return Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        workspace=WorkspaceKind.WORKTREE,
    )


def _ok(operation: Operation, environment: Environment) -> OperationResult:
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"ran": operation.kind},
        rendering="done",
    )


# --- schema version ---------------------------------------------------------


def test_all_three_contract_types_carry_a_schema_version(env: Environment) -> None:
    """The cross-repo contract versions without a flag day."""
    operation = Operation(kind="test.thing")
    result = OperationResult(operation_id=operation.id, status=OperationStatus.SUCCEEDED)

    assert operation.schema_version == SCHEMA_VERSION
    assert result.schema_version == SCHEMA_VERSION
    assert env.schema_version == SCHEMA_VERSION

    for payload in (operation.to_dict(), result.to_dict(), env.to_dict()):
        assert payload["schema_version"] == SCHEMA_VERSION


# --- Operation --------------------------------------------------------------


def test_apply_defaults_to_false() -> None:
    """Imported callers must state apply=True; there is no implicit apply."""
    assert Operation(kind="fs.write").apply is False


def test_operation_ids_are_stable_and_unique() -> None:
    """An id is minted once, survives derivation, and is never shared.

    Stability is asserted across the derivations that really occur — a JSON
    round-trip and a field-level ``replace`` — because that is where an id could
    actually be lost. An earlier version read ``operation.id`` twice and compared
    the results, which a frozen dataclass field cannot fail: it asserted nothing.

    This matters beyond tidiness. The id is the key an evidence record is filed
    under, so an operation that acquires a new one partway through the pipeline
    detaches its record from what the caller asked for. ``apply_rewrite`` refuses
    a rewrite that changes ``id`` for the same reason.
    """
    operation = Operation(kind="fs.read")

    assert Operation.from_dict(operation.to_dict()).id == operation.id
    assert replace(operation, apply=True).id == operation.id

    assert Operation(kind="fs.read").id != operation.id


def test_operation_round_trips_through_json() -> None:
    operation = Operation(
        kind="process.exec",
        arguments={"argv": ["python", "-m", "pytest"]},
        profile=ExecutionProfile.PROJECT,
        apply=True,
        caller={"agent": "consumer", "task_id": "t1", "tool": "run_tests"},
        timeout_seconds=30.0,
    )
    payload = json.loads(json.dumps(operation.to_dict()))
    restored = Operation.from_dict(payload)

    assert restored == operation


def test_resource_request_falls_back_to_environment_defaults(env: Environment) -> None:
    unset = Operation(kind="process.exec")
    assert unset.resolved_timeout(env) == DEFAULT_TIMEOUT_SECONDS
    assert unset.resolved_max_output_bytes(env) == DEFAULT_MAX_OUTPUT_BYTES

    explicit = Operation(kind="process.exec", timeout_seconds=5.0, max_output_bytes=10)
    assert explicit.resolved_timeout(env) == 5.0
    assert explicit.resolved_max_output_bytes(env) == 10


def test_unresolved_intent_is_treated_as_requiring_apply() -> None:
    """Unknown is the conservative side of "does this change anything?"."""
    assert Operation(kind="mystery").requires_apply is True


# --- OperationResult: a preview is never a success --------------------------


def test_result_statuses_are_exactly_the_five_contract_states() -> None:
    assert {status.value for status in OperationStatus} == {
        "previewed",
        "denied",
        "succeeded",
        "failed",
        "timed_out",
    }


@pytest.mark.parametrize(
    "status",
    [
        OperationStatus.PREVIEWED,
        OperationStatus.DENIED,
        OperationStatus.FAILED,
        OperationStatus.TIMED_OUT,
    ],
)
def test_non_success_statuses_are_false_by_every_predicate(status: OperationStatus) -> None:
    result = OperationResult(operation_id="x", status=status)
    assert result.succeeded is False
    assert bool(result) is False
    assert result.previewed is (status is OperationStatus.PREVIEWED)
    assert result.denied is (status is OperationStatus.DENIED)


def test_succeeded_is_the_only_truthy_status() -> None:
    result = OperationResult(operation_id="x", status=OperationStatus.SUCCEEDED)
    assert result.succeeded is True
    assert bool(result) is True
    assert result.previewed is False
    assert result.denied is False


def test_effects_are_incomplete_until_a_handler_says_otherwise() -> None:
    """The honest default: an effect list may be partial unless claimed complete."""
    result = OperationResult(operation_id="x", status=OperationStatus.SUCCEEDED)
    assert result.effects_complete is False
    assert result.effects.complete is False

    observed = OperationResult(
        operation_id="x",
        status=OperationStatus.SUCCEEDED,
        effects=Effects(changed_paths=("a.txt",), bytes_written=3, complete=True),
    )
    assert observed.effects_complete is True


def test_result_is_json_serializable() -> None:
    result = OperationResult(
        operation_id="x",
        status=OperationStatus.FAILED,
        error="boom",
        effects=Effects(changed_paths=("a.txt",)),
        evidence=Evidence(backend="host", exit_code=1, stdout="out", stderr="err"),
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["status"] == "failed"
    assert payload["effects"]["changed_paths"] == ["a.txt"]
    # stdout and stderr are captured separately, never pre-merged.
    assert payload["evidence"]["stdout"] == "out"
    assert payload["evidence"]["stderr"] == "err"


def test_ungated_is_distinct_from_allowed() -> None:
    """An absent gate is not a gate that permitted the operation."""
    assert PolicyVerdict().decision is PolicyDecision.UNGATED
    assert PolicyVerdict().denied is False
    assert PolicyVerdict(decision=PolicyDecision.DENIED).denied is True


# --- Environment: two independent axes --------------------------------------


@pytest.mark.parametrize("workspace", list(WorkspaceKind))
@pytest.mark.parametrize("runner", [HostRunner(), _FakeContainerRunner()])
def test_workspace_and_runner_axes_vary_independently(
    tmp_path: Path, workspace: WorkspaceKind, runner: Runner
) -> None:
    """All four combinations are constructible; neither axis constrains the other."""
    environment = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=runner,
        workspace=workspace,
    )
    assert environment.workspace is workspace
    assert environment.runner is runner


def test_environment_separates_trusted_control_context_from_the_work_root(
    tmp_path: Path,
) -> None:
    environment = Environment(
        source_root=tmp_path / "checkout",
        work_root=tmp_path / "worktree",
        runner=HostRunner(),
    )
    assert environment.roots_are_separate is True
    assert environment.source_root != environment.work_root

    same = Environment(source_root=tmp_path, work_root=tmp_path, runner=HostRunner())
    assert same.roots_are_separate is False


def test_environment_resolves_roots_to_absolute_paths(tmp_path: Path) -> None:
    environment = Environment(
        source_root=tmp_path / "a" / ".." / "a",
        work_root=tmp_path / "a",
        runner=HostRunner(),
        read_only_paths=(tmp_path / "a" / "vendor",),
    )
    assert environment.source_root.is_absolute()
    assert environment.source_root == (tmp_path / "a").resolve()
    assert all(path.is_absolute() for path in environment.read_only_paths)


def test_host_environment_does_not_claim_to_enforce_a_network_policy(
    tmp_path: Path,
) -> None:
    """A declared deny that nothing enforces must not read as a control."""
    environment = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=HostRunner(),
        network=NetworkPolicy.DENY,
    )
    assert environment.network is NetworkPolicy.DENY
    assert environment.network_enforced is False

    enforcing = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=_FakeContainerRunner(),
        network=NetworkPolicy.DENY,
    )
    assert enforcing.network_enforced is True


def test_environment_default_network_posture_is_unrestricted(tmp_path: Path) -> None:
    environment = Environment(source_root=tmp_path, work_root=tmp_path, runner=HostRunner())
    assert environment.network is NetworkPolicy.UNRESTRICTED


def test_environment_records_secret_names_only(tmp_path: Path) -> None:
    environment = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=HostRunner(),
        secret_names=("GH_TOKEN",),
    )
    payload = environment.to_dict()
    assert payload["secret_names"] == ["GH_TOKEN"]
    assert "value" not in json.dumps(payload)


def test_environment_is_json_serializable(env: Environment) -> None:
    payload = json.loads(json.dumps(env.to_dict()))
    assert payload["workspace"] == "worktree"
    assert payload["runner"]["name"] == "host"


# --- HostRunner: a guard, not a sandbox -------------------------------------


def test_host_runner_satisfies_the_runner_protocol() -> None:
    assert isinstance(HostRunner(), Runner)


def test_host_runner_reports_no_isolation() -> None:
    runner = HostRunner()
    assert runner.name == "host"
    assert runner.isolation == "none"
    assert "not a sandbox" in runner.isolation_note.lower()
    assert runner.describe()["isolation"] == "none"


# --- the lifecycle pipeline -------------------------------------------------


def test_reads_execute_immediately_without_apply(registry, env: Environment) -> None:
    calls: list[str] = []

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        calls.append(operation.kind)
        return _ok(operation, environment)

    registry("test.read", intent=OperationIntent.OBSERVE, run=_run)

    result = operations.execute(Operation(kind="test.read"), env)
    assert result.status is OperationStatus.SUCCEEDED
    assert calls == ["test.read"]


@pytest.mark.parametrize(
    "intent",
    [OperationIntent.MUTATE, OperationIntent.EXECUTE, OperationIntent.LIFECYCLE],
)
def test_mutation_and_execution_preview_by_default(
    registry, env: Environment, intent: OperationIntent
) -> None:
    calls: list[str] = []

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        calls.append(operation.kind)
        return _ok(operation, environment)

    kind = f"test.{intent.value}"
    registry(kind, intent=intent, run=_run)

    result = operations.execute(Operation(kind=kind), env)

    assert result.status is OperationStatus.PREVIEWED
    assert result.previewed is True
    assert result.succeeded is False
    assert bool(result) is False
    assert calls == [], "a preview must not reach the handler"


def test_a_preview_makes_no_effect_claim(registry, env: Environment) -> None:
    registry("test.write", intent=OperationIntent.MUTATE, run=_ok)

    result = operations.execute(Operation(kind="test.write"), env)

    assert result.effects.changed_paths == ()
    assert result.effects.bytes_written == 0
    assert result.effects_complete is False, (
        "a preview describes what would run; claiming a complete effect list "
        "would be predicting effects it never observed"
    )


def test_apply_true_reaches_the_handler(registry, env: Environment) -> None:
    calls: list[bool] = []

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        calls.append(operation.apply)
        return _ok(operation, environment)

    registry("test.apply", intent=OperationIntent.MUTATE, run=_run)

    result = operations.execute(Operation(kind="test.apply", apply=True), env)
    assert result.status is OperationStatus.SUCCEEDED
    assert calls == [True]


def test_unknown_kind_is_a_failed_result_not_an_exception(env: Environment) -> None:
    result = operations.execute(Operation(kind="test.never-registered"), env)
    assert result.status is OperationStatus.FAILED
    assert "no handler registered" in result.error


def test_a_caller_cannot_relabel_a_mutation_as_an_observation(registry, env: Environment) -> None:
    """Intent is declared by the handler, so it cannot be used to skip preview."""
    calls: list[str] = []

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        calls.append(operation.kind)
        return _ok(operation, environment)

    registry("test.sneaky", intent=OperationIntent.MUTATE, run=_run)

    result = operations.execute(Operation(kind="test.sneaky", intent=OperationIntent.OBSERVE), env)

    assert result.status is OperationStatus.FAILED
    assert calls == []
    assert "declared 'mutate'" in result.error


def test_normalize_fills_intent_and_profile_from_the_registration(registry) -> None:
    registry(
        "test.normalize",
        intent=OperationIntent.EXECUTE,
        profile=ExecutionProfile.PROJECT,
        run=_ok,
    )

    normalized = operations.normalize(Operation(kind="test.normalize"))
    assert normalized.intent is OperationIntent.EXECUTE
    assert normalized.profile is ExecutionProfile.PROJECT


def test_normalize_keeps_a_caller_supplied_profile(registry) -> None:
    """Profile is a caller decision; intent is not."""
    registry(
        "test.profile",
        intent=OperationIntent.EXECUTE,
        profile=ExecutionProfile.PROJECT,
        run=_ok,
    )

    normalized = operations.normalize(
        Operation(kind="test.profile", profile=ExecutionProfile.CONTROL)
    )
    assert normalized.profile is ExecutionProfile.CONTROL


def test_a_handler_crash_becomes_a_recoverable_failed_result(registry, env: Environment) -> None:
    def _boom(operation: Operation, environment: Environment) -> OperationResult:
        raise RuntimeError("handler exploded")

    registry("test.boom", intent=OperationIntent.OBSERVE, run=_boom)

    result = operations.execute(Operation(kind="test.boom"), env)

    assert result.status is OperationStatus.FAILED
    assert "RuntimeError" in result.error
    assert "handler exploded" in result.error


def test_registering_a_kind_twice_is_an_error(registry) -> None:
    registry("test.dup", intent=OperationIntent.OBSERVE, run=_ok)
    with pytest.raises(ValueError, match="already registered"):
        operations.register(
            "test.dup",
            intent=OperationIntent.OBSERVE,
            default_profile=ExecutionProfile.OBSERVE,
            run=_ok,
        )


# --- the policy seam --------------------------------------------------------


def test_an_operation_with_no_policy_configured_reports_ungated(registry, env: Environment) -> None:
    """``UNGATED`` is deliberately not ``ALLOWED``.

    Renamed from ``test_no_policy_evaluator_yet_reports_ungated``: the evaluator
    now exists and is wired into dispatch, so the old name asserted something
    that had stopped being true. What it actually pins — no policy configured
    means no gate, distinct from a gate that permitted the operation — is
    unchanged. The gate's own behaviour lives in ``test_dispatch_policy.py``.
    """
    registry("test.ungated", intent=OperationIntent.OBSERVE, run=_ok)

    result = operations.execute(Operation(kind="test.ungated"), env)
    assert result.verdict.decision is PolicyDecision.UNGATED


def test_the_policy_gate_runs_before_the_preview_branch(
    registry, env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied operation reports DENIED even when the caller only previewed.

    Handing back a preview would imply the operation would otherwise have run.
    """
    calls: list[str] = []

    def _run(operation: Operation, environment: Environment) -> OperationResult:
        calls.append(operation.kind)
        return _ok(operation, environment)

    registry("test.denied", intent=OperationIntent.MUTATE, run=_run)
    monkeypatch.setattr(
        operations,
        "_policy_gate",
        lambda operation, environment: PolicyVerdict(
            decision=PolicyDecision.DENIED, reason="denied by test", matched_rule="deny:*"
        ),
    )

    previewed = operations.execute(Operation(kind="test.denied"), env)
    applied = operations.execute(Operation(kind="test.denied", apply=True), env)

    for result in (previewed, applied):
        assert result.status is OperationStatus.DENIED
        assert result.succeeded is False
        assert bool(result) is False
        assert result.verdict.matched_rule == "deny:*"
    assert calls == [], "a denied operation must never reach the handler"


# --- evidence ---------------------------------------------------------------


def test_every_result_carries_the_runners_isolation_posture(registry, env: Environment) -> None:
    """The posture travels in the payload, not only in prose a consumer may skip."""
    registry("test.evidence", intent=OperationIntent.OBSERVE, run=_ok)

    result = operations.execute(Operation(kind="test.evidence"), env)
    evidence = result.evidence

    assert evidence.backend == "host"
    assert evidence.isolation == "none"
    assert "not a sandbox" in evidence.isolation_note.lower()
    assert evidence.environment_id == env.id
    assert evidence.workspace_kind == "worktree"
    assert evidence.root == str(env.work_root)


@pytest.mark.parametrize("apply_it", [False, True])
def test_previews_and_failures_carry_evidence_too(
    registry, env: Environment, apply_it: bool
) -> None:
    registry("test.always-evidence", intent=OperationIntent.MUTATE, run=_ok)

    result = operations.execute(Operation(kind="test.always-evidence", apply=apply_it), env)
    assert result.evidence.backend == "host"
    assert result.evidence.duration_ms is not None
    assert result.evidence.duration_ms >= 0.0


def test_unknown_kind_failure_still_carries_evidence(env: Environment) -> None:
    result = operations.execute(Operation(kind="test.nope"), env)
    assert result.evidence.environment_id == env.id
    assert result.evidence.started_at is not None


def test_the_pipeline_owns_where_facts_and_the_handler_owns_what_facts(
    registry, env: Environment
) -> None:
    """A handler cannot misreport the environment it ran in."""

    def _lying(operation: Operation, environment: Environment) -> OperationResult:
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(
                backend="somewhere-else",
                isolation="total",
                exit_code=0,
                stdout="captured",
            ),
        )

    registry("test.liar", intent=OperationIntent.OBSERVE, run=_lying)

    evidence = operations.execute(Operation(kind="test.liar"), env).evidence

    assert evidence.backend == "host", "the pipeline stamps the runner, not the handler"
    assert evidence.isolation == "none"
    # What only the handler can know survives untouched.
    assert evidence.exit_code == 0
    assert evidence.stdout == "captured"


def test_evidence_records_that_the_host_enforces_no_network_policy(
    registry, tmp_path: Path
) -> None:
    environment = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=HostRunner(),
        network=NetworkPolicy.DENY,
    )
    registry("test.network", intent=OperationIntent.OBSERVE, run=_ok)

    evidence = operations.execute(Operation(kind="test.network"), environment).evidence
    assert evidence.network == "deny"
    assert evidence.network_enforced is False


# --- the empty package markers ----------------------------------------------


@pytest.mark.parametrize("module_name", ["shell.fs", "shell.process"])
def test_handler_packages_predeclare_no_exports(module_name: str) -> None:
    """r8: no fake or stub exports before the modules behind them exist.

    Checked against ``__all__`` and against non-module attributes only. Once a
    real handler module lands (``shell.fs.write``, for instance) and anything
    in the process imports it — exactly what "imported by explicit module
    path" requires — Python's own import machinery binds it onto the parent
    package object (``shell.fs.write`` becomes ``vars(shell.fs)["write"]``).
    That is ordinary, unavoidable import behaviour for a real submodule, not
    the fake/stub export this test exists to catch, and it must not depend on
    which other test modules happened to run first in this process. What r8
    actually forbids is ``__init__.py`` itself declaring or curating a
    re-export surface — so a *non-module* public attribute (a name r8 would
    forbid, e.g. a stub function or curated re-export) still fails this test.
    """
    import importlib
    import types

    module = importlib.import_module(module_name)
    assert module.__all__ == ()
    public = [
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
    ]
    assert public == [], f"{module_name} predeclares exports: {public}"
