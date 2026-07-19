"""The policy gate inside operation dispatch — the extraction's sharpest edge.

The first consumer's enforcement ordering is ``pre_tool hook (deny/rewrite) ->
policy -> execute``, and the single most security-relevant line in it
(``colleague/loop.py:962``) re-wraps the call so the gate judges the **rewritten**
arguments. An extraction that gates the original while running the rewritten form
reintroduces a bypass: a rewrite turns a denied command into an allowed shape and
the gate never sees it.

These tests pin that property from both ends, and pin it as *structure* rather
than as a behaviour that happens to hold today:

* the gate is inside :func:`shell.operations.execute`, so a caller that skips
  every orchestration layer is still gated;
* the operation the gate judged and the operation the handler ran are the same
  object, so no refactor can quietly separate them;
* a rewrite may change arguments and nothing else — a kind change would move the
  operation out of its own gate's jurisdiction;
* an untrustworthy policy fails closed, within that jurisdiction and not beyond;
* the filesystem carve-out inherited from the first consumer is preserved;
* every operation leaves an evidence record, denials included.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from shell import operations
from shell.environment import Environment
from shell.evidence import EvidenceRecord, EvidenceStore
from shell.operations import (
    ExecutionProfile,
    Operation,
    OperationIntent,
    RewriteRejected,
    apply_rewrite,
)
from shell.policy import Policy, PolicyCandidate, load_policy
from shell.results import OperationResult, OperationStatus, PolicyDecision
from shell.runners.host import HostRunner


@pytest.fixture
def registry() -> Iterator[Callable[..., str]]:
    """Register throwaway operation kinds and clean them up afterwards."""
    registered: list[str] = []

    def _register(
        kind: str,
        *,
        intent: OperationIntent = OperationIntent.EXECUTE,
        run: Callable[[Operation, Environment], OperationResult],
        profile: ExecutionProfile = ExecutionProfile.PROJECT,
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
    return Environment(source_root=source, work_root=work, runner=HostRunner())


class _Recorder:
    """A handler that records every operation it was handed."""

    def __init__(self) -> None:
        self.seen: list[Operation] = []

    def __call__(self, operation: Operation, environment: Environment) -> OperationResult:
        self.seen.append(operation)
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.SUCCEEDED,
            output={"arguments": dict(operation.arguments)},
            rendering="ran",
        )

    @property
    def commands(self) -> list[Any]:
        return [op.arguments.get("command") for op in self.seen]


def _policy(**sections: Any) -> Policy:
    """An inline policy with the given sections present."""
    return load_policy(data=sections)


def _allow_only_git() -> Policy:
    return _policy(run_command={"allow": ["git"], "deny": []})


def _deny_rm() -> Policy:
    return _policy(run_command={"allow": [], "deny": ["rm"]})


# --- the gate is inside dispatch --------------------------------------------


def test_a_direct_caller_with_no_orchestration_is_still_gated(registry, env: Environment) -> None:
    """The gate is in ``execute``, not in a wrapper a caller can decline to use.

    This is the whole reason the evaluation moved inside dispatch. A consumer
    importing shell-cli and calling the pipeline directly — no hooks, no loop, no
    adapter — gets the operator's verdict, because there is no other entry point.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=_deny_rm(),
    )

    assert result.status is OperationStatus.DENIED
    assert result.verdict.decision is PolicyDecision.DENIED
    assert result.verdict.matched_rule == "run_command.deny"
    assert "rm" in result.error
    assert handler.seen == [], "a denied operation must never reach the handler"


def test_denial_is_its_own_status_not_a_failure(registry, env: Environment) -> None:
    """``DENIED`` is distinct from ``FAILED`` and is not a success by any predicate."""
    registry("process.shell", run=_Recorder())

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm x"}, apply=True),
        env,
        policy=_deny_rm(),
    )

    assert result.status is OperationStatus.DENIED
    assert result.status is not OperationStatus.FAILED
    assert result.succeeded is False
    assert bool(result) is False
    assert result.denied is True
    # The reason is model-visible: it reaches the consumer on the result itself.
    assert result.rendering == result.verdict.reason
    assert result.verdict.reason


def test_an_allowed_operation_reaches_the_handler(registry, env: Environment) -> None:
    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.ALLOWED
    assert handler.commands == ["git status"]


# --- the rewrite seam: both directions --------------------------------------


def test_a_rewrite_from_allowed_to_denied_is_caught(registry, env: Environment) -> None:
    """The bypass this whole slice exists to prevent.

    The caller asks for something the operator permits; a rewrite turns it into
    something forbidden. A gate that judged the original would wave it through.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_deny_rm(),
        rewrite=lambda op: {"command": "rm -rf /"},
    )

    assert result.status is OperationStatus.DENIED
    assert result.verdict.matched_rule == "run_command.deny"
    assert "rm" in result.verdict.reason
    assert handler.seen == [], "the rewritten command was denied and must not have run"


def test_a_rewrite_from_denied_to_allowed_is_re_evaluated_not_stale(
    registry, env: Environment
) -> None:
    """The other direction, which fails quietly rather than loudly.

    A stale verdict here is *conservative* — it would deny something now
    permitted — but it is still wrong, and wrong in a way nobody reports as a
    security bug: the recorded verdict would describe a command that never ran.
    The gate must judge what executes, so the verdict tracks the rewrite.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=_deny_rm(),
        rewrite=lambda op: {"command": "git status"},
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.ALLOWED
    assert result.verdict.decision is not PolicyDecision.DENIED
    assert handler.commands == ["git status"], "the handler must run the rewritten command"


def test_the_gated_operation_and_the_executed_operation_are_one_object(
    registry, env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural pin: identity, not equality.

    Equality would still hold if a future refactor gated a faithful *copy* of the
    original and ran the rewritten form. Identity does not: it fails the moment
    the two stop being the same value, which is exactly the class of bug
    ``loop.py:962`` exists to prevent.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)

    gated: list[Operation] = []
    real_gate = operations._policy_gate

    def _spy(operation: Operation, policy: Policy):
        gated.append(operation)
        return real_gate(operation, policy)

    monkeypatch.setattr(operations, "_policy_gate", _spy)

    operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: {"command": "git diff"},
    )

    assert len(gated) == 1 and len(handler.seen) == 1
    assert gated[0] is handler.seen[0]
    assert gated[0].arguments["command"] == "git diff"


def test_the_preview_branch_also_sees_the_rewritten_operation(registry, env: Environment) -> None:
    """A preview must describe what would actually run, rewrite included."""
    registry("process.shell", run=_Recorder())

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: {"command": "git diff"},
    )

    assert result.status is OperationStatus.PREVIEWED
    assert result.output["arguments"] == {"command": "git diff"}


def test_a_rewrite_returning_none_leaves_the_operation_alone(registry, env: Environment) -> None:
    handler = _Recorder()
    registry("process.shell", run=handler)

    operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: None,
    )

    assert handler.commands == ["git status"]


def test_a_rewrite_that_raises_is_a_failed_result_not_an_exception(
    registry, env: Environment
) -> None:
    """A consumer's hook is not trusted to behave; the agent loop must survive it."""
    handler = _Recorder()
    registry("process.shell", run=handler)

    def _boom(operation: Operation) -> dict[str, Any]:
        raise RuntimeError("hook exploded")

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=_boom,
    )

    assert result.status is OperationStatus.FAILED
    assert "hook exploded" in result.error
    assert handler.seen == []


# --- a rewrite may change arguments and nothing else -------------------------


def test_a_rewrite_may_not_change_the_operation_kind(registry, env: Environment) -> None:
    """A kind change would move the operation out of its own gate's jurisdiction.

    ``process.shell`` is gated by the ``run_command`` policy; ``fs.example`` is
    deliberately not gated at all. If a rewrite could relabel one as the other it
    would not be bending the rule, it would be selecting which rule applies.
    """
    shell_handler = _Recorder()
    fs_handler = _Recorder()
    registry("process.shell", run=shell_handler)
    registry("fs.example", intent=OperationIntent.OBSERVE, run=fs_handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=_deny_rm(),
        rewrite=lambda op: Operation(
            kind="fs.example",
            arguments={"command": "rm -rf /"},
            intent=op.intent,
            profile=op.profile,
            apply=op.apply,
            id=op.id,
        ),
    )

    assert result.status is OperationStatus.FAILED
    assert "may not change the operation kind" in result.error
    assert shell_handler.seen == []
    assert fs_handler.seen == [], "the relabelled kind must not have been dispatched either"


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("apply", True),
        ("intent", OperationIntent.OBSERVE),
        ("profile", ExecutionProfile.CONTROL),
        ("timeout_seconds", 9999.0),
        ("max_output_bytes", 10**9),
        ("caller", {"agent": "someone-else"}),
    ],
)
def test_a_rewrite_may_not_change_anything_but_arguments(
    registry, env: Environment, field_name: str, value: Any
) -> None:
    """Every non-argument field is out of reach, not just ``kind``.

    ``apply`` is the sharpest of these: a rewrite that flipped it would turn a
    preview the caller deliberately requested into a real mutation.
    """
    from dataclasses import replace

    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: replace(op, **{field_name: value}),
    )

    assert result.status is OperationStatus.FAILED
    assert "arguments only" in result.error
    assert handler.seen == []


def test_a_rewrite_may_not_mint_a_new_operation_id(registry, env: Environment) -> None:
    """The id is the key the evidence record is filed under.

    A rewrite that replaced it would detach the record of what happened from what
    the caller asked for, which is an audit failure even when nothing unsafe ran.
    """
    registry("process.shell", run=_Recorder())

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: Operation(
            kind=op.kind,
            arguments={"command": "git diff"},
            intent=op.intent,
            profile=op.profile,
            apply=op.apply,
        ),
    )

    assert result.status is OperationStatus.FAILED
    assert "arguments only" in result.error


def test_the_mapping_form_has_no_channel_for_a_kind_change(env: Environment) -> None:
    """The safer of the two rewrite shapes, checked directly.

    A rewrite that returns a mapping cannot express a kind change at all — a
    ``kind`` key lands in ``arguments`` as ordinary data. This is the shape the
    first consumer's hook already produces.
    """
    original = Operation(kind="process.shell", arguments={"command": "git status"})

    rewritten = apply_rewrite(original, {"kind": "fs.example", "command": "git diff"})

    assert rewritten.kind == "process.shell"
    assert rewritten.arguments == {"kind": "fs.example", "command": "git diff"}
    assert rewritten.id == original.id


def test_the_operation_form_is_accepted_when_only_arguments_differ(
    registry, env: Environment
) -> None:
    """The permissive half of the operation-form rule, which must still work.

    A rewriter that prefers to hand back a whole operation may, and its arguments
    are gated exactly like the mapping form's. Only the fields it left alone are
    what make it acceptable.
    """
    from dataclasses import replace

    handler = _Recorder()
    registry("process.shell", run=handler)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: replace(op, arguments={"command": "git status"}),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.ALLOWED
    assert handler.commands == ["git status"]


def test_apply_rewrite_rejects_a_kind_change_loudly() -> None:
    original = Operation(kind="process.shell", arguments={})

    with pytest.raises(RewriteRejected, match="may not change the operation kind"):
        apply_rewrite(original, Operation(kind="fs.example", arguments={}, id=original.id))


# --- an untrustworthy policy fails closed, within its jurisdiction -----------


def _malformed_policy(tmp_path: Path) -> Policy:
    broken = tmp_path / "approvals.json"
    broken.write_text("{ not json at all", encoding="utf-8")
    return load_policy([broken])


def _unresolved_policy(tmp_path: Path) -> Policy:
    missing = tmp_path / "never-written.json"
    return load_policy([PolicyCandidate(path=missing, required=True)])


@pytest.mark.parametrize("make_policy", [_malformed_policy, _unresolved_policy])
def test_an_untrustworthy_policy_denies_a_gated_operation(
    registry, env: Environment, tmp_path: Path, make_policy
) -> None:
    """Fail closed. A gate that could not be read is not permission.

    The evaluator deliberately does not deny on its own behalf — it reports
    ``trustworthy`` and leaves the consequence to its caller. Dispatch is that
    caller, and this is the decision it makes.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)
    policy = make_policy(tmp_path)
    assert policy.trustworthy is False

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=policy,
    )

    assert result.status is OperationStatus.DENIED
    assert result.verdict.matched_rule == "policy.untrustworthy"
    assert "fails closed" in result.verdict.reason
    assert handler.seen == []


def test_an_untrustworthy_policy_does_not_deny_a_carved_out_operation(
    registry, env: Environment, tmp_path: Path
) -> None:
    """Fail closed inside the gate's jurisdiction, and not one inch beyond it.

    No ``approvals.json``, however well-formed, can gate a structured file read —
    the first consumer excludes file tool calls from the policy on purpose. So a
    corrupt policy file cannot have been withholding permission for one, and
    denying it would be enforcing a rule that could never have existed. The
    degradation is still reported on the verdict rather than passing in silence.
    """
    handler = _Recorder()
    registry("fs.example", intent=OperationIntent.OBSERVE, run=handler)

    result = operations.execute(
        Operation(kind="fs.example", arguments={"path": "README.md"}),
        env,
        policy=_malformed_policy(tmp_path),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.UNGATED
    assert "policy degraded" in result.verdict.reason
    assert len(handler.seen) == 1


def test_an_absent_policy_is_ungated_and_stays_distinct_from_a_broken_one(
    registry, env: Environment, tmp_path: Path
) -> None:
    """Presence is the semantic. Absent must never collapse into malformed.

    An absent policy is a strict no-op — the repository behaves as it did before
    a gate existed. A malformed one is a declared gate that failed to load. They
    produce opposite outcomes here, and that is the point.
    """
    registry("process.shell", run=_Recorder())
    operation = Operation(kind="process.shell", arguments={"command": "git status"}, apply=True)

    absent = operations.execute(operation, env, policy=load_policy([tmp_path / "nope.json"]))
    broken = operations.execute(operation, env, policy=_malformed_policy(tmp_path))

    assert absent.status is OperationStatus.SUCCEEDED
    assert absent.verdict.decision is PolicyDecision.UNGATED
    assert broken.status is OperationStatus.DENIED


def test_no_policy_argument_behaves_exactly_like_an_empty_policy(
    registry, env: Environment
) -> None:
    """Omitting the policy is not a way to switch a configured gate off.

    It cannot be: the gate a caller would be disabling is the one it also has to
    supply. Omission means nothing was declared, which is the empty policy.
    """
    registry("process.shell", run=_Recorder())
    operation = Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True)

    omitted = operations.execute(operation, env)
    empty = operations.execute(operation, env, policy=Policy())

    assert omitted.verdict.decision is PolicyDecision.UNGATED
    assert empty.verdict.decision is PolicyDecision.UNGATED
    assert omitted.status is empty.status is OperationStatus.SUCCEEDED


# --- jurisdiction: what the run_command gate does and does not cover ---------


def test_the_filesystem_carve_out_is_preserved(registry, env: Environment) -> None:
    """File operations are not routed through the command allow-list.

    Inherited from the first consumer, which pins it in its own suite. It is a
    deliberate boundary, not an oversight: confining file operations is the
    filesystem layer's job, and a checksum-or-token allow-list over every read
    would be a different product.
    """
    handler = _Recorder()
    registry("fs.example", intent=OperationIntent.OBSERVE, run=handler)

    # A policy that denies everything it can: an empty allow list that is present.
    result = operations.execute(
        Operation(kind="fs.example", arguments={"path": "x", "command": "rm -rf /"}),
        env,
        policy=_policy(run_command={"allow": ["git"], "deny": ["rm"]}),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.verdict.decision is PolicyDecision.UNGATED
    assert "not subject to the run_command policy" in result.verdict.reason


def test_an_argv_operation_is_gated_by_its_program_token(registry, env: Environment) -> None:
    """Project execution must not escape the gate by arriving as argv.

    ``process.exec`` carries a vector rather than a shell string, and a gate that
    only understood strings would let every curated invocation through unchecked.
    """
    handler = _Recorder()
    registry("process.exec", run=handler)

    denied = operations.execute(
        Operation(kind="process.exec", arguments={"argv": ["rm", "-rf", "/"]}, apply=True),
        env,
        policy=_deny_rm(),
    )
    allowed = operations.execute(
        Operation(kind="process.exec", arguments={"argv": ["git", "status"]}, apply=True),
        env,
        policy=_allow_only_git(),
    )

    assert denied.status is OperationStatus.DENIED
    assert denied.verdict.matched_rule == "run_command.deny"
    assert allowed.status is OperationStatus.SUCCEEDED


def test_an_argv_element_containing_spaces_cannot_forge_a_program_token(
    registry, env: Environment
) -> None:
    """Joining argv must quote, or an argument could impersonate the program.

    ``["git", "; rm -rf /"]`` naively joined would re-tokenize into something the
    gate reads differently from what would run.
    """
    registry("process.exec", run=_Recorder())

    result = operations.execute(
        Operation(kind="process.exec", arguments={"argv": ["rm", "&& git"]}, apply=True),
        env,
        policy=_allow_only_git(),
    )

    assert result.status is OperationStatus.DENIED
    assert "'rm'" in result.verdict.reason


def test_a_gated_operation_with_no_command_is_denied_under_a_present_section(
    registry, env: Environment
) -> None:
    """Nothing to approve means no approval. Allow-lists deny by default."""
    registry("process.shell", run=_Recorder())

    result = operations.execute(
        Operation(kind="process.shell", arguments={}, apply=True),
        env,
        policy=_allow_only_git(),
    )

    assert result.status is OperationStatus.DENIED
    assert "no program token" in result.verdict.reason


# --- evidence: every operation leaves a record ------------------------------


def _sink() -> tuple[list[EvidenceRecord], Callable[[EvidenceRecord], None]]:
    records: list[EvidenceRecord] = []
    return records, records.append


def test_a_denied_operation_still_produces_an_evidence_record(registry, env: Environment) -> None:
    """The audit hole this package exists to close.

    A refusal that leaves no trace is indistinguishable from an operation nobody
    ever attempted, which makes the gate unauditable exactly where it mattered.
    """
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    operations.execute(
        Operation(
            kind="process.shell",
            arguments={"command": "rm -rf /"},
            apply=True,
            caller={"agent": "colleague", "tool": "run_command"},
        ),
        env,
        policy=_deny_rm(),
        evidence_sink=sink,
    )

    assert len(records) == 1
    body = records[0].to_dict()
    assert body["status"] == "denied"
    assert body["policy"]["decision"] == "denied"
    assert body["policy"]["matched_rule"] == "run_command.deny"
    assert body["caller"]["agent"] == "colleague"
    assert body["execution"]["applied"] is False


def test_a_denied_operation_is_never_recorded_as_applied(registry, env: Environment) -> None:
    """``apply=True`` on a denied operation is an intention, not an event.

    Regression pin. Deriving ``applied`` from the caller's request alone recorded
    every denied mutation as having been applied — an auditor reading the record
    would conclude the refused command ran. The gate refuses before the handler,
    so nothing was applied no matter how the caller asked.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)
    records, sink = _sink()

    operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=_deny_rm(),
        evidence_sink=sink,
    )

    execution = records[0].to_dict()["execution"]
    assert execution["applied"] is False
    assert execution["previewed"] is False
    # The caller's intent is still recorded — it is just not confused with the fact.
    assert execution["requested_apply"] is True
    assert handler.seen == []


# --- applied: the pre-handler / in-handler distinction ----------------------
#
# ``failed`` covers two situations with opposite answers to "did anything
# happen?". The tests below are named for that split on purpose: a future editor
# cannot collapse them back into one boolean without the diff saying so.


def _rewrite_to_other_kind(op: Operation) -> Operation:
    from dataclasses import replace

    return replace(op, kind="fs.example")


def _rewrite_that_raises(op: Operation) -> dict:
    raise RuntimeError("hook exploded")


@pytest.mark.parametrize(
    "label, kind, kwargs",
    [
        ("unknown kind, normalize refused", "nobody.registered.this", {}),
        ("rewrite rejected", "process.shell", {"rewrite": _rewrite_to_other_kind}),
        ("rewrite raised", "process.shell", {"rewrite": _rewrite_that_raises}),
    ],
)
def test_a_failure_before_the_handler_was_entered_is_never_applied(
    registry, env: Environment, label: str, kind: str, kwargs: dict
) -> None:
    """Nothing ran, so ``applied`` is definitively false — not merely unknown.

    Every one of these returns from ``execute`` *above* ``spec.run``. The first
    fix caught the denial path and left these three reporting ``applied=True``:
    a rewrite rejected for trying to turn ``process.shell`` into ``fs.example``
    would have been filed as an operation that was carried out.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)
    registry("fs.example", intent=OperationIntent.OBSERVE, run=_Recorder())
    records, sink = _sink()

    result = operations.execute(
        Operation(kind=kind, arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_sink=sink,
        **kwargs,
    )

    assert result.status is OperationStatus.FAILED, label
    execution = records[0].to_dict()["execution"]
    assert execution["applied"] is False, label
    assert execution["handler_entered"] is False, label
    assert execution["handler_disposition"] == "not_reached", label
    assert execution["requested_apply"] is True, label
    assert handler.seen == [], label


def test_a_crash_inside_the_handler_records_applied_as_unknown(registry, env: Environment) -> None:
    """``applied`` is null here, and null is the only honest value.

    The handler was entered and died partway. It may have written half a file,
    started a process, or done nothing at all — and nothing at this layer can
    tell which. ``false`` would be a fabricated all-clear; ``true`` would be a
    fabricated change. The record declines to guess, in a field an auditor can
    filter on rather than in prose.
    """
    entered: list[Operation] = []

    def _crash(operation: Operation, environment: Environment) -> OperationResult:
        entered.append(operation)
        raise RuntimeError("died mid-write")

    registry("process.shell", run=_crash)
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_sink=sink,
    )

    assert result.status is OperationStatus.FAILED
    assert len(entered) == 1, "the handler really was entered"

    execution = records[0].to_dict()["execution"]
    assert execution["applied"] is None
    assert execution["applied"] is not False, "false here would be a fabricated all-clear"
    assert execution["handler_entered"] is True
    assert execution["handler_disposition"] == "crashed"


def test_the_two_kinds_of_failure_stay_distinguishable(registry, env: Environment) -> None:
    """Both are ``failed``; the record must not let that be the whole story.

    This is the assertion that stops the distinction being quietly collapsed:
    two operations with identical status carry different answers to "was
    anything applied?", and neither answer is invented.
    """

    def _crash(operation: Operation, environment: Environment) -> OperationResult:
        raise RuntimeError("died mid-write")

    registry("process.shell", run=_crash)
    records, sink = _sink()

    before = operations.execute(Operation(kind="gone.missing", apply=True), env, evidence_sink=sink)
    inside = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_sink=sink,
    )

    assert before.status is inside.status is OperationStatus.FAILED
    before_exec = records[0].to_dict()["execution"]
    inside_exec = records[1].to_dict()["execution"]

    assert before_exec["applied"] is False
    assert inside_exec["applied"] is None
    assert before_exec["handler_entered"] != inside_exec["handler_entered"]
    assert before_exec["handler_disposition"] != inside_exec["handler_disposition"]


@pytest.mark.parametrize(
    "label, kind, apply_it, gate, status, applied, entered",
    [
        ("succeeded", "process.shell", True, "allow", "succeeded", True, True),
        ("previewed", "process.shell", False, "allow", "previewed", False, False),
        ("denied", "process.shell", True, "deny", "denied", False, False),
        ("failed", "gone.missing", True, "allow", "failed", False, False),
    ],
)
def test_every_terminal_state_reports_applied_honestly(
    registry,
    env: Environment,
    label: str,
    kind: str,
    apply_it: bool,
    gate: str,
    status: str,
    applied: bool,
    entered: bool,
) -> None:
    """All four terminal states in one table, so none of them drifts alone."""
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    command = "rm -rf /" if gate == "deny" else "git status"
    result = operations.execute(
        Operation(kind=kind, arguments={"command": command}, apply=apply_it),
        env,
        policy=_deny_rm() if gate == "deny" else _allow_only_git(),
        evidence_sink=sink,
    )

    assert result.status.value == status, label
    execution = records[0].to_dict()["execution"]
    assert execution["applied"] is applied, label
    assert execution["handler_entered"] is entered, label
    # The caller's request survives regardless — it is a separate fact.
    assert execution["requested_apply"] is apply_it, label


def test_a_record_built_outside_dispatch_declines_to_guess() -> None:
    """The out-of-pipeline default degrades safely rather than asserting.

    ``build_record`` called directly cannot know how far a pipeline got, so a
    non-success status yields ``applied=None``. Assuming it ran is the dangerous
    direction; the default picks "unknown" instead.
    """
    from shell.evidence import build_record

    operation = Operation(kind="process.shell", arguments={"command": "x"}, apply=True)
    failed = build_record(
        OperationResult(operation_id=operation.id, status=OperationStatus.FAILED),
        requested=operation,
    ).to_dict()["execution"]
    succeeded = build_record(
        OperationResult(operation_id=operation.id, status=OperationStatus.SUCCEEDED),
        requested=operation,
    ).to_dict()["execution"]

    assert failed["applied"] is None
    assert failed["handler_entered"] is None
    assert failed["handler_disposition"] == "unstated"
    # A success can only have come from a handler that ran, so that much is safe.
    assert succeeded["applied"] is True


@pytest.mark.parametrize(
    "kind, arguments, apply_it, expected",
    [
        ("process.shell", {"command": "git status"}, True, "succeeded"),
        ("process.shell", {"command": "git status"}, False, "previewed"),
        ("process.shell", {"command": "rm x"}, True, "denied"),
        ("nobody.registered.this", {}, True, "failed"),
    ],
)
def test_every_terminal_state_produces_a_record(
    registry, env: Environment, kind: str, arguments: dict, apply_it: bool, expected: str
) -> None:
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    operations.execute(
        Operation(kind=kind, arguments=arguments, apply=apply_it),
        env,
        policy=_policy(run_command={"allow": ["git"], "deny": ["rm"]}),
        evidence_sink=sink,
    )

    assert len(records) == 1
    assert records[0].status == expected


def test_the_record_holds_both_the_requested_and_the_rewritten_operation(
    registry, env: Environment
) -> None:
    """An auditor needs to see what was asked for as well as what ran.

    A record holding only the executed form cannot answer "did a hook change
    this?", which is the first question anyone asks about a rewrite.
    """
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        rewrite=lambda op: {"command": "git diff"},
        evidence_sink=sink,
    )

    body = records[0].to_dict()
    assert body["operation"]["requested"]["arguments"] == {"command": "git status"}
    assert body["operation"]["normalized"]["arguments"] == {"command": "git diff"}
    assert body["operation"]["normalized_available"] is True


def test_an_unknown_kind_records_that_normalization_never_happened(
    env: Environment,
) -> None:
    """Recorded as absent rather than by copying the requested form into both slots."""
    records, sink = _sink()

    operations.execute(Operation(kind="nope.nothing"), env, evidence_sink=sink)

    body = records[0].to_dict()
    assert body["operation"]["normalized"] is None
    assert body["operation"]["normalized_available"] is False


def test_a_configured_store_persists_the_record(registry, env: Environment) -> None:
    """A denied operation lands on disk, under the trusted source root.

    Note the asymmetry the persisted body shows: the record on disk carries no
    ``persistence`` block, because a record cannot describe the outcome of its
    own write from inside the bytes being written. Being readable there *is* the
    persistence fact; the returned record is where the outcome is reported.
    """
    registry("process.shell", run=_Recorder())
    store = EvidenceStore.for_environment(env)
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "rm x"}, apply=True),
        env,
        policy=_deny_rm(),
        evidence_store=store,
        evidence_sink=sink,
    )

    assert result.status is OperationStatus.DENIED
    assert result.evidence.degraded is False

    stored = store.records()
    assert len(stored) == 1
    assert stored[0]["status"] == "denied"
    assert "persistence" not in stored[0]

    persistence = records[0].to_dict()["persistence"]
    assert persistence["persisted"] is True
    assert persistence["path"].endswith(".json")
    # The store sits under source_root, the operation may only write work_root.
    assert persistence["store_outside_work_root"] is True


def test_declared_secrets_never_reach_the_record(registry, env: Environment) -> None:
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    operations.execute(
        Operation(
            kind="process.shell",
            arguments={"command": "git push https://user:hunter2@example.invalid"},
            apply=True,
        ),
        env,
        policy=_allow_only_git(),
        evidence_sink=sink,
        secrets={"TOKEN": "hunter2"},
    )

    serialized = json.dumps(records[0].to_dict())
    assert "hunter2" not in serialized
    assert "[redacted]" in serialized
    assert records[0].to_dict()["redaction"]["secret_names"] == ["TOKEN"]
    # Never claimed clean: undeclared secrets are not detected.
    assert records[0].redaction_complete is False


def test_a_failed_write_degrades_the_evidence_without_losing_the_outcome(
    registry, env: Environment, tmp_path: Path
) -> None:
    """An action that ran and could not be recorded is not a clean run.

    It is also not a failed operation. The handler succeeded; what failed was the
    paperwork, and the result says both.
    """
    registry("process.shell", run=_Recorder())
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_store=EvidenceStore(directory=blocker),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.evidence.degraded is True
    assert "could not be persisted" in result.evidence.degraded_reason


def test_a_sink_that_raises_degrades_the_evidence_and_never_the_operation(
    registry, env: Environment
) -> None:
    """Bookkeeping must not overturn an outcome that already happened."""
    handler = _Recorder()
    registry("process.shell", run=handler)

    def _hostile(record: EvidenceRecord) -> None:
        raise RuntimeError("sink exploded")

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_sink=_hostile,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert len(handler.seen) == 1
    assert result.evidence.degraded is True
    assert "sink exploded" in result.evidence.degraded_reason


def test_a_record_that_cannot_be_built_degrades_rather_than_raising(
    registry, env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the record-builder failing must not turn into an exception.

    By the time evidence is assembled the operation has already happened.
    Raising here would replace a real outcome with an error about the paperwork,
    and the agent loop would lose the thing it needed to react to.
    """
    handler = _Recorder()
    registry("process.shell", run=handler)

    def _broken(*args: Any, **kwargs: Any):
        raise RuntimeError("record builder exploded")

    monkeypatch.setattr(operations, "capture", _broken)

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert len(handler.seen) == 1
    assert result.evidence.degraded is True
    assert "record builder exploded" in result.evidence.degraded_reason


def test_without_a_store_the_record_is_built_but_not_called_degraded(
    registry, env: Environment
) -> None:
    """No store is not a failure — the record reached its caller.

    Persistence is opt-in, and its absence is reported as a plain fact on the
    record rather than as evidence degradation, which is reserved for a trail
    that was supposed to exist and does not.
    """
    registry("process.shell", run=_Recorder())
    records, sink = _sink()

    result = operations.execute(
        Operation(kind="process.shell", arguments={"command": "git status"}, apply=True),
        env,
        policy=_allow_only_git(),
        evidence_sink=sink,
    )

    assert result.evidence.degraded is False
    assert records[0].to_dict()["persistence"]["persisted"] is False
