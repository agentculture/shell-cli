"""The redaction boundary: which representations of an operation hold a secret.

An operation produces two things a secret can survive in — the persisted
**evidence record** and the live **OperationResult** handed back to the caller.
Redacting one and not the other is not a partial win, it is a misplaced one: the
record is read by an auditor after the fact, while the result is what the first
consumer renders straight back to a model. Protecting only the record protects
the reader who was never at risk.

These tests pin the boundary from both sides:

* a declared secret is removed from **both** representations by default;
* the removal is deep — output payloads, renderings, errors, policy reasons and
  effect lists, not just captured stdout;
* a caller may opt into seeing declared secrets in the *result*, and that opt-in
  never reaches the *record*;
* nothing anywhere claims the job is complete, because an undeclared secret is
  still recorded verbatim.

The last one is the point of the whole design. ``tests/test_evidence.py`` asserts
the undeclared leak directly so that anyone later claiming broader coverage has to
edit a test that states the limitation in writing; this file keeps that claim
aligned across the two representations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from shell import operations
from shell.environment import Environment
from shell.evidence import REDACTED, EvidenceRecord, EvidenceStore
from shell.operations import ExecutionProfile, Operation, OperationIntent
from shell.policy import load_policy
from shell.results import (
    Effects,
    Evidence,
    OperationResult,
    OperationStatus,
    PolicyDecision,
    SecretHandling,
)
from shell.runners.host import HostRunner

SECRET = "sk-live-DEADBEEF-do-not-leak"  # nosec B105 - test fixture, not a credential
OTHER = "pw-SECOND-SECRET-value"  # nosec B105 - test fixture, not a credential


@pytest.fixture()
def env(tmp_path: Path) -> Environment:
    work = tmp_path / "work"
    work.mkdir()
    return Environment(
        runner=HostRunner(),
        source_root=tmp_path / "checkout",
        work_root=work,
        env_passthrough=("PATH",),
    )


@pytest.fixture()
def registry() -> Iterator[Any]:
    """Install a controllable handler, shadowing a real kind if one is registered.

    These tests need a handler that returns a secret on demand, under a kind the
    ``run_command`` policy has jurisdiction over — so they use the real
    ``process.shell`` name. ``shell/process/shell.py`` registers that name on
    import, and :func:`shell.operations.register` refuses a duplicate, so any
    test session that also imports the real module (which under ``pytest -n auto``
    depends on how tests are distributed across workers) would collide.

    Shadow and restore, following ``tests/test_dispatch_policy.py``: renaming to a
    fake kind would move these tests off the kinds that actually matter.
    Restoration is unconditional so a later test in the same worker still sees the
    real handler.
    """
    registered: list[str] = []
    shadowed: dict[str, Any] = {}

    def _register(kind: str, run: Any, intent: OperationIntent = OperationIntent.EXECUTE) -> None:
        try:
            shadowed[kind] = operations.handler_for(kind)
        except operations.UnknownOperationKind:
            pass
        else:
            operations.unregister(kind)
        operations.register(
            kind,
            intent=intent,
            default_profile=ExecutionProfile.PROJECT,
            run=run,
        )
        registered.append(kind)

    yield _register

    for kind in registered:
        operations.unregister(kind)
    for kind, spec in shadowed.items():
        operations.register(
            kind, intent=spec.intent, default_profile=spec.default_profile, run=spec.run
        )


def _sink() -> tuple[list[EvidenceRecord], Any]:
    records: list[EvidenceRecord] = []
    return records, records.append


# --- the default: both representations are scrubbed -------------------------


def test_a_declared_secret_is_removed_from_the_live_result_not_only_the_record(
    registry, env: Environment
) -> None:
    """The gap this file exists for, pinned from the result's side.

    Previously ``execute`` passed ``secrets`` to ``capture()`` alone, so the
    record came back clean while ``result.evidence.stdout`` held the value
    verbatim. Since the first consumer renders result output back to the model,
    that was the one path a declared secret was guaranteed to travel.
    """

    def _leaky(operation: Operation, environment: Environment) -> OperationResult:
        # The handler is never given the secrets and does not know one is here;
        # it simply reports what a process wrote.
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"token={SECRET}\n", stderr=f"warn: {SECRET}"),
            rendering=f"exit=0 token={SECRET}",
            output={"token": SECRET},
        )

    registry("process.shell", run=_leaky)
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "mint"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
        evidence_sink=sink,
    )

    assert result.status is OperationStatus.SUCCEEDED

    # Every string-bearing surface of the live result.
    assert SECRET not in result.evidence.stdout
    assert SECRET not in result.evidence.stderr
    assert SECRET not in result.rendering
    assert result.output["token"] == REDACTED
    assert SECRET not in json.dumps(result.to_dict(), default=str)

    # And the record, which was already true and must stay true.
    assert SECRET not in json.dumps(records[0].to_dict(), default=str)


def test_the_result_reports_that_it_was_scrubbed(registry, env: Environment) -> None:
    """A reader must not have to infer redaction from the absence of a value."""

    def _leaky(operation: Operation, environment: Environment) -> OperationResult:
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"{SECRET} {SECRET}"),
        )

    registry("process.shell", run=_leaky)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "mint"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
    )

    assert result.evidence.secret_handling is SecretHandling.REDACTED
    assert result.evidence.secret_replacements == 2
    assert result.evidence.redaction_complete is False
    assert result.to_dict()["evidence"]["secret_handling"] == "redacted"


def test_no_declared_secrets_is_a_distinct_state_from_having_scrubbed_them(
    registry, env: Environment
) -> None:
    """``none_declared`` and ``redacted`` are different facts and stay different."""
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id, status=OperationStatus.SUCCEEDED, evidence=Evidence(stdout="hi")
        ),
    )

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "echo hi"}, apply=True),
        env,
    )

    assert result.evidence.secret_handling is SecretHandling.NONE_DECLARED
    assert result.evidence.secret_replacements == 0
    # Still never a claim of cleanliness: nothing was declared, so nothing was
    # checked, which is the weakest possible position rather than the strongest.
    assert result.evidence.redaction_complete is False


def test_redaction_reaches_every_surface_not_just_captured_output(
    registry, env: Environment
) -> None:
    """A field-by-field allow-list would be a list somebody forgets to extend."""

    def _everywhere(operation: Operation, environment: Environment) -> OperationResult:
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.FAILED,
            error=f"failed while using {SECRET}",
            rendering=f"render {SECRET}",
            output={"nested": {"deep": [f"list {SECRET}"]}, SECRET: "secret-as-a-key"},
            effects=Effects(changed_paths=(f"/tmp/{SECRET}.txt",), complete=False),  # nosec B108
            evidence=Evidence(
                stdout=f"out {SECRET}",
                stderr=f"err {SECRET}",
                degraded=True,
                degraded_reason=f"degraded because of {SECRET}",
            ),
        )

    registry("process.shell", run=_everywhere)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "x"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
    )

    serialized = json.dumps(result.to_dict(), default=str)
    assert SECRET not in serialized

    # Spot-check the shapes survived, so this is redaction and not deletion.
    assert result.output["nested"]["deep"] == [f"list {REDACTED}"]
    assert REDACTED in result.output  # the key itself was scrubbed
    assert result.effects.changed_paths == (f"/tmp/{REDACTED}.txt",)  # nosec B108
    assert isinstance(result.effects.changed_paths, tuple), "a tuple field must stay a tuple"
    assert result.evidence.degraded is True, "scrubbing must not clear an unrelated flag"


def test_scrubbing_does_not_demote_enum_fields(registry, env: Environment) -> None:
    """This package's enums subclass ``str``, so a naive walk would flatten them.

    ``OperationStatus.SUCCEEDED`` is a ``str`` instance. A redactor treating every
    string as scrubbable would return a bare ``"succeeded"``, breaking every
    ``is``-comparison downstream — including the ones this suite relies on.
    """
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=SECRET),
        ),
    )

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=load_policy(data={"run_command": {"allow": ["git"], "deny": []}}),
        secrets={"API_TOKEN": SECRET},
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.ALLOWED
    assert result.evidence.secret_handling is SecretHandling.REDACTED


def test_a_longer_secret_containing_a_shorter_one_is_fully_removed(
    registry, env: Environment
) -> None:
    """Longest-first ordering, pinned through the result rather than the record."""
    short = "DEADBEEF"  # nosec B105 - test fixture
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"long={SECRET} short={short}"),
        ),
    )

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "x"}, apply=True),
        env,
        secrets={"LONG": SECRET, "SHORT": short},
    )

    assert SECRET not in result.evidence.stdout
    assert short not in result.evidence.stdout


# --- the opt-in, and its limits ---------------------------------------------


def test_a_caller_may_opt_into_seeing_declared_secrets_in_the_result(
    registry, env: Environment
) -> None:
    """The named opt-in, for the caller whose output legitimately IS the secret.

    A command that mints a token has to be able to hand it back. What that caller
    does NOT get is a quiet exemption: the flag is explicit, it is off by default,
    and the result says ``revealed`` so the exposure is recorded as a choice.
    """
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"token={SECRET}"),
        ),
    )

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "mint"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
        reveal_secrets_in_result=True,
    )

    assert SECRET in result.evidence.stdout
    assert result.evidence.secret_handling is SecretHandling.REVEALED
    assert result.evidence.secret_replacements == 0
    # Even here, no claim of completeness is made in either direction.
    assert result.evidence.redaction_complete is False


def test_the_opt_in_never_reaches_the_evidence_record(registry, env: Environment) -> None:
    """The audit trail is not negotiable. This is the asymmetry that makes the
    opt-in safe to offer at all: the caller can see a minted token live, and the
    durable record still must not hold it.
    """
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"token={SECRET}"),
        ),
    )
    store = EvidenceStore.for_environment(env)
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "mint"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
        reveal_secrets_in_result=True,
        evidence_store=store,
        evidence_sink=sink,
    )

    assert SECRET in result.evidence.stdout, "the caller asked to see it"
    assert SECRET not in json.dumps(records[0].to_dict(), default=str)

    # And not in the bytes on disk either — the record is what an auditor reads.
    stored = store.records()
    assert len(stored) == 1
    assert SECRET not in json.dumps(stored[0], default=str)


def test_the_opt_in_defaults_to_off(registry, env: Environment) -> None:
    """A caller who forgets the flag gets redaction, never exposure.

    Pinned as a signature fact rather than only a behavioural one, so a future
    change of the default has to walk past an assertion about the default itself.
    """
    import inspect

    default = inspect.signature(operations.execute).parameters["reveal_secrets_in_result"].default
    assert default is False


# --- the limit that stays -----------------------------------------------------


def test_an_undeclared_secret_is_still_present_in_both_representations(
    registry, env: Environment
) -> None:
    """The honest gap, asserted rather than described.

    Scrubbing the live result does NOT widen the guarantee. Only *declared*
    values are removed; a credential a command printed on its own is not
    detected, in the record or in the result. There is no pattern library here on
    purpose — a heuristic catching some undeclared secrets would invite callers
    to stop declaring them, converting a visible gap into an invisible one.

    If a future change makes this test fail, the claim in
    ``docs/evidence-contract.md`` has to change with it.
    """
    undeclared = "undeclared-credential-value"  # nosec B105 - test fixture
    registry(
        "process.shell",
        run=lambda op, e: OperationResult(
            operation_id=op.id,
            status=OperationStatus.SUCCEEDED,
            evidence=Evidence(stdout=f"{SECRET} {undeclared}"),
        ),
    )
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "x"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
        evidence_sink=sink,
    )

    assert SECRET not in result.evidence.stdout, "the declared one goes"
    assert undeclared in result.evidence.stdout, "the undeclared one stays"
    assert undeclared in json.dumps(records[0].to_dict(), default=str)

    # And the record still refuses to call itself clean.
    assert records[0].redaction_complete is False
    assert result.evidence.redaction_complete is False


def test_secrets_never_reach_a_handler(registry, env: Environment) -> None:
    """Scrubbing the result must not have widened what a handler can see.

    Redaction happens after the handler returns, so declaring a secret is not a
    way to hand one to project code. The handler signature takes an operation and
    an environment; there is nowhere for a secret value to arrive.
    """
    seen: list[tuple[Any, ...]] = []

    def _spy(operation: Operation, environment: Environment) -> OperationResult:
        seen.append((operation, environment))
        return OperationResult(operation_id=operation.id, status=OperationStatus.SUCCEEDED)

    registry("process.shell", run=_spy)

    operations.execute(
        Operation(kind="process.shell", arguments={"command": "x"}, apply=True),
        env,
        secrets={"API_TOKEN": SECRET},
    )

    operation, environment = seen[0]
    assert SECRET not in json.dumps(operation.to_dict(), default=str)
    assert SECRET not in json.dumps(environment.to_dict(), default=str)
    # The environment carries secret NAMES only, and not even those unless declared.
    assert SECRET not in str(getattr(environment, "secret_names", ()))
