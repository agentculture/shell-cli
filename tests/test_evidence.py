"""Contract tests for the evidence record.

These run against **synthetic** results. Real process execution and real
filesystem effects arrive in a later slice; what is pinned here is the shape of
the record, the scope of redaction, the versioning, and — the part that matters
most — that a record which could not be written comes back marked degraded
rather than quietly missing.

The redaction tests deserve a note, because one of them asserts a *leak*. A
declared secret is removed; an undeclared secret a command printed is recorded
verbatim. That asymmetry is the honest description of what this module does, and
it is asserted rather than documented-and-forgotten so that anyone who later
claims broader coverage has to come here and change a test that says, in
writing, what the gap is.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from shell.environment import Environment, NetworkPolicy, WorkspaceKind
from shell.evidence import (
    DEFAULT_STORE_SUBDIR,
    PERSISTENCE_UNKNOWN_NOTE,
    REDACTED,
    REDACTION_IS_COMPLETE,
    EvidenceRecord,
    EvidenceStore,
    Redactor,
    RetentionPolicy,
    build_record,
    capture,
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
from shell.runners.host import HostRunner

# --- synthetic fixtures -----------------------------------------------------


def make_operation(**overrides: Any) -> Operation:
    defaults: dict[str, Any] = {
        "kind": "process.exec",
        "arguments": {"argv": ["python", "-m", "pytest"]},
        "apply": True,
        "caller": {"agent": "colleague", "task_id": "t-77", "tool": "run_tests"},
        "timeout_seconds": 30.0,
    }
    defaults.update(overrides)
    return Operation(**defaults)


def make_result(operation: Operation, **evidence_overrides: Any) -> OperationResult:
    """A synthetic succeeded result with a fully-populated evidence block."""
    evidence_fields: dict[str, Any] = {
        "backend": "host",
        "isolation": "none",
        "isolation_note": "Host execution is a guard, not a sandbox.",
        "environment_id": "env-1",
        "workspace_kind": "worktree",
        "root": "/tmp/work",
        "cwd": "/tmp/work",
        "network": "unrestricted",
        "network_enforced": False,
        "mounts": (),
        "started_at": 1000.0,
        "ended_at": 1002.5,
        "duration_ms": 2500.0,
        "exit_code": 0,
        "stdout": "all tests passed",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_bytes": 16,
        "stderr_bytes": 0,
    }
    evidence_fields.update(evidence_overrides)
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"exit_code": 0},
        rendering="all tests passed",
        verdict=PolicyVerdict(
            decision=PolicyDecision.ALLOWED,
            reason="matched an allow rule",
            matched_rule="run_command.allow[3]",
        ),
        effects=Effects(changed_paths=("a.py",), bytes_written=12, complete=True),
        evidence=Evidence(**evidence_fields),
    )


@pytest.fixture()
def environment(tmp_path: Path) -> Environment:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        workspace=WorkspaceKind.WORKTREE,
        network=NetworkPolicy.DENY,
        secret_names=("API_TOKEN",),
    )


# --- 1. redaction scope: what is removed, and what provably is not ----------


def test_declared_secret_is_redacted_everywhere_it_appears() -> None:
    operation = make_operation(arguments={"argv": ["deploy", "--token", "SEKRET-VALUE"]})
    result = make_result(operation, stdout="using SEKRET-VALUE now", stderr="SEKRET-VALUE again")

    record = build_record(
        result, requested=operation, redactor=Redactor(secrets={"API_TOKEN": "SEKRET-VALUE"})
    )

    blob = json.dumps(record.to_dict())
    assert "SEKRET-VALUE" not in blob, "a declared secret value reached the record"
    assert REDACTED in record.to_dict()["output"]["stdout"]["text"]
    # The name travels; only the value is withheld.
    assert record.to_dict()["redaction"]["secret_names"] == ["API_TOKEN"]
    assert record.to_dict()["redaction"]["replacements"] == 3


def test_undeclared_secret_is_NOT_redacted() -> None:
    """The documented gap, asserted deliberately.

    shell-cli redacts values it was told about. A credential a command printed
    on its own is indistinguishable from ordinary output at this layer, and no
    pattern heuristic is applied — a scanner that caught *some* undeclared
    secrets would invite callers to stop declaring them, which is strictly worse
    than a gap everyone can see.
    """
    operation = make_operation()
    result = make_result(
        operation,
        stdout="declared=DECLARED-abc leaked=ghp_UNDECLARED_TOKEN_xyz",
    )

    record = build_record(
        result, requested=operation, redactor=Redactor(secrets={"KNOWN": "DECLARED-abc"})
    )
    stdout = record.to_dict()["output"]["stdout"]["text"]

    assert "DECLARED-abc" not in stdout
    assert "ghp_UNDECLARED_TOKEN_xyz" in stdout, (
        "This assertion documents a real limitation. If redaction ever grows to "
        "cover undeclared secrets, update the contract and REDACTION_IS_COMPLETE "
        "together with this test — do not simply delete it."
    )


def test_the_record_never_claims_redaction_is_complete() -> None:
    operation = make_operation()
    record = build_record(make_result(operation), requested=operation)

    assert REDACTION_IS_COMPLETE is False
    assert record.redaction_complete is False
    assert record.to_dict()["redaction"]["complete"] is False


def test_no_input_can_make_redaction_claim_completeness() -> None:
    """Even declaring every secret in the output leaves the marker False."""
    operation = make_operation()
    result = make_result(operation, stdout="a b c")
    record = build_record(
        result,
        requested=operation,
        redactor=Redactor(secrets={"A": "a", "B": "b", "C": "c"}),
    )
    assert record.to_dict()["redaction"]["complete"] is False


def test_redaction_scope_is_the_whole_body_not_a_field_list() -> None:
    """A declared secret cannot hide in a field the scrubber forgot to visit."""
    secret = "SEKRET-VALUE"
    operation = make_operation(
        arguments={"nested": {"deep": [secret]}},
        caller={"agent": "colleague", "note": secret},
    )
    result = OperationResult(
        operation_id=operation.id,
        status=OperationStatus.FAILED,
        output={"detail": secret},
        rendering=f"failed: {secret}",
        error=f"boom {secret}",
        verdict=PolicyVerdict(decision=PolicyDecision.DENIED, reason=f"denied {secret}"),
        evidence=Evidence(stdout=secret, stderr=secret, degraded_reason=secret),
    )

    record = build_record(result, requested=operation, redactor=Redactor(secrets={"S": secret}))
    assert secret not in json.dumps(record.to_dict())


def test_a_secret_used_as_a_dict_key_is_redacted() -> None:
    secret = "SEKRET-VALUE"
    operation = make_operation(arguments={secret: "value"})
    record = build_record(
        make_result(operation), requested=operation, redactor=Redactor(secrets={"S": secret})
    )
    assert secret not in json.dumps(record.to_dict())


def test_overlapping_secrets_redact_longest_first() -> None:
    """A shorter secret must not consume a longer one and leave its tail exposed."""
    operation = make_operation()
    result = make_result(operation, stdout="prefix-AND-SUFFIX here")
    record = build_record(
        result,
        requested=operation,
        redactor=Redactor(secrets={"SHORT": "prefix", "LONG": "prefix-AND-SUFFIX"}),
    )
    assert "SUFFIX" not in json.dumps(record.to_dict())


def test_empty_secret_values_are_ignored() -> None:
    """An empty declared value must not explode into a redaction of everything."""
    operation = make_operation()
    record = build_record(
        make_result(operation), requested=operation, redactor=Redactor(secrets={"EMPTY": ""})
    )
    assert record.to_dict()["output"]["stdout"]["text"] == "all tests passed"


def test_redactor_reports_names_but_never_values() -> None:
    redactor = Redactor(secrets={"B_TOKEN": "v2", "A_TOKEN": "v1"})
    assert redactor.names == ("A_TOKEN", "B_TOKEN")


# --- 2. schema_version and versioning ---------------------------------------


def test_schema_version_is_readable_off_all_three_surfaces() -> None:
    operation = make_operation()
    result = make_result(operation)
    record = build_record(result, requested=operation)

    assert operation.schema_version == SCHEMA_VERSION
    assert result.schema_version == SCHEMA_VERSION
    assert record.schema_version == SCHEMA_VERSION


def test_schema_version_survives_serialization(tmp_path: Path) -> None:
    """The version is readable from the persisted bytes, not just the object."""
    operation = make_operation()
    store = EvidenceStore(directory=tmp_path / "evidence")
    _, record = capture(make_result(operation), requested=operation, store=store)

    on_disk = json.loads(store.paths()[0].read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk["operation"]["requested"]["schema_version"] == SCHEMA_VERSION
    assert record.schema_version == on_disk["schema_version"]


# --- 3. degraded evidence: a failed write is never a silent success ---------


def test_forced_write_failure_marks_the_result_degraded(tmp_path: Path) -> None:
    """The acceptance test for this slice.

    The store directory path is occupied by a *file*, so ``mkdir`` cannot create
    it. Chosen over ``chmod`` because a suite running as root would sail through
    a permission bit and the test would pass without ever exercising failure.
    """
    blocker = tmp_path / "evidence"
    blocker.write_text("not a directory", encoding="utf-8")

    operation = make_operation()
    result = make_result(operation)
    assert result.evidence.degraded is False

    returned, record = capture(result, requested=operation, store=EvidenceStore(directory=blocker))

    assert returned.evidence.degraded is True
    assert "could not be persisted" in returned.evidence.degraded_reason
    assert returned.status is OperationStatus.SUCCEEDED, (
        "the operation itself still succeeded — degraded evidence describes the "
        "record, and must not rewrite the outcome of the work"
    )
    assert record.to_dict()["persistence"]["persisted"] is False


def test_write_failure_midway_is_caught_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the directory exists is handled the same way."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)

    operation = make_operation()
    store = EvidenceStore(directory=tmp_path / "evidence")
    returned, record = capture(make_result(operation), requested=operation, store=store)

    assert returned.evidence.degraded is True
    assert "disk full" in returned.evidence.degraded_reason
    assert record.to_dict()["persistence"]["persisted"] is False
    # The partial file is cleaned up rather than left to be read as a record.
    assert store.paths() == []


def test_degraded_reason_is_appended_not_overwritten(tmp_path: Path) -> None:
    """An already-degraded result keeps its original reason alongside the new one."""
    blocker = tmp_path / "evidence"
    blocker.write_text("blocked", encoding="utf-8")

    operation = make_operation()
    result = make_result(operation, degraded=True, degraded_reason="stdout capture was lost")

    returned, _ = capture(result, requested=operation, store=EvidenceStore(directory=blocker))

    assert "stdout capture was lost" in returned.evidence.degraded_reason
    assert "could not be persisted" in returned.evidence.degraded_reason


def test_a_secret_in_a_degraded_reason_is_redacted(tmp_path: Path) -> None:
    """The failure path must not become the leak path."""
    blocker = tmp_path / "SEKRET-VALUE"
    blocker.write_text("blocked", encoding="utf-8")

    operation = make_operation()
    returned, _ = capture(
        make_result(operation),
        requested=operation,
        store=EvidenceStore(directory=blocker),
        secrets={"S": "SEKRET-VALUE"},
    )

    assert returned.evidence.degraded is True
    assert "SEKRET-VALUE" not in returned.evidence.degraded_reason


def test_a_successful_write_leaves_the_result_untouched(tmp_path: Path) -> None:
    operation = make_operation()
    result = make_result(operation)
    store = EvidenceStore(directory=tmp_path / "evidence")

    returned, record = capture(result, requested=operation, store=store)

    assert returned is result, "a clean capture must not rebuild the result"
    assert returned.evidence.degraded is False
    assert record.to_dict()["persistence"]["persisted"] is True


#: Every block ``docs/evidence-contract.md`` documents as part of "the record".
#: The round-trip test below compares the bytes on disk against this set, which is
#: the assertion whose absence let the persisted body silently lose a section.
DOCUMENTED_BLOCKS = frozenset(
    {
        "schema_version",
        "record_id",
        "recorded_at",
        "operation_id",
        "status",
        "caller",
        "operation",
        "execution",
        "policy",
        "environment",
        "output",
        "effects",
        "redaction",
        "evidence_quality",
        "persistence",
        "integrity",
    }
)


def test_a_record_read_back_off_disk_has_every_documented_block(tmp_path: Path) -> None:
    """Round-trip: write a record, read the bytes back, compare to the contract.

    This is the test whose absence hid a real divergence. Every other assertion in
    this file inspects the **in-memory** record ``capture`` returns, and the
    in-memory record was complete — so a persisted body missing an entire
    documented section went unnoticed until a CLI verb tried to read one.

    ``EvidenceStore.write`` serialises the record it is handed, so any block added
    to the returned record *after* the write is absent from the artifact. Asserting
    against the documented key set, from disk, is what makes that class of drift
    impossible to reintroduce quietly.
    """
    operation = make_operation()
    store = EvidenceStore(directory=tmp_path / "evidence")

    _, record = capture(make_result(operation), requested=operation, store=store)

    stored = store.records()
    assert len(stored) == 1
    on_disk = stored[0]

    assert set(on_disk) == DOCUMENTED_BLOCKS, "the stored body must match the documented contract"
    # The in-memory record and the artifact agree on which blocks exist. They are
    # allowed to differ on one field's *value* — see the test below — never on
    # whether a reader finds the block at all.
    assert set(record.to_dict()) == set(on_disk)


def test_the_stored_body_reports_its_own_write_as_unknown_rather_than_omitting_it(
    tmp_path: Path,
) -> None:
    """The one field an artifact genuinely cannot fill, and how it says so.

    A record is the thing being written, so the outcome of that write does not
    exist when the bytes are serialised. Two dishonest options were available:
    omit the block (a reader silently misses a documented section) or assert
    ``persisted: true`` before knowing it (a claim about the future). It reports
    ``null`` with a note instead, which is the same posture ``execution.applied``
    and ``effects.complete`` take elsewhere.

    Everything knowable *before* the write — the destination path, whether the
    store sits outside the work root — is filled in, so the null is narrow.
    """
    operation = make_operation()
    store = EvidenceStore(directory=tmp_path / "evidence")

    _, record = capture(make_result(operation), requested=operation, store=store)

    on_disk = store.records()[0]["persistence"]
    assert on_disk["persisted"] is None
    assert on_disk["write_attempted"] is True
    assert on_disk["path"].endswith(".json")
    assert PERSISTENCE_UNKNOWN_NOTE in on_disk["note"]

    # The returned record resolves it; the two disagree only here, and only in
    # the direction of the artifact knowing less.
    returned = record.to_dict()["persistence"]
    assert returned["persisted"] is True
    assert returned["path"] == on_disk["path"]
    assert returned["note"] == ""


def test_the_stored_bodys_digest_validates_against_its_own_bytes(tmp_path: Path) -> None:
    """Adding the persistence block before the write must not break integrity.

    ``integrity.content_sha256`` covers the canonical serialization of the rest of
    the body. The on-disk body and the returned record now have *different*
    persistence blocks and therefore different digests — which is correct, they
    are different bodies. What must hold is that each digest validates against the
    bytes it accompanies, so an external validator reading the file recomputes a
    match.
    """
    operation = make_operation()
    store = EvidenceStore(directory=tmp_path / "evidence")
    capture(make_result(operation), requested=operation, store=store)

    on_disk = store.records()[0]
    claimed = on_disk.pop("integrity")["content_sha256"]

    recomputed = hashlib.sha256(
        json.dumps(on_disk, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    assert recomputed == claimed


def test_no_store_is_not_degraded() -> None:
    """Handing the record back to the caller is itself a delivery channel."""
    operation = make_operation()
    returned, record = capture(make_result(operation), requested=operation)

    assert returned.evidence.degraded is False
    payload = record.to_dict()["persistence"]
    assert payload["persisted"] is False
    assert "no evidence store" in payload["reason"]


# --- 4. the minimum field set -----------------------------------------------


def test_record_carries_every_required_field(environment: Environment) -> None:
    """CLAUDE.md's "Evidence is a product surface" list, checked field by field."""
    operation = make_operation()
    normalized = operation.__class__(
        kind=operation.kind,
        arguments=operation.arguments,
        intent=OperationIntent.EXECUTE,
        profile=ExecutionProfile.PROJECT,
        apply=True,
        caller=operation.caller,
        timeout_seconds=operation.timeout_seconds,
        id=operation.id,
    )
    record = build_record(
        make_result(operation),
        requested=operation,
        normalized=normalized,
        environment=environment,
    )
    payload = record.to_dict()

    # operation id, caller, task and tool
    assert payload["operation_id"] == operation.id
    assert payload["caller"]["agent"] == "colleague"
    assert payload["caller"]["task_id"] == "t-77"
    assert payload["caller"]["tool"] == "run_tests"

    # requested and normalized operation
    assert payload["operation"]["requested"]["kind"] == "process.exec"
    assert payload["operation"]["normalized"]["intent"] == "execute"
    assert payload["operation"]["normalized"]["profile"] == "project"
    assert payload["operation"]["normalized_available"] is True

    # preview/applied state
    assert payload["execution"]["applied"] is True
    assert payload["execution"]["previewed"] is False

    # policy verdict and matched rule
    assert payload["policy"]["decision"] == "allowed"
    assert payload["policy"]["matched_rule"] == "run_command.allow[3]"

    # environment id, workspace kind, runner, root and cwd
    assert payload["environment"]["id"] == "env-1"
    assert payload["environment"]["workspace_kind"] == "worktree"
    assert payload["environment"]["runner"] == "host"
    assert payload["environment"]["root"] == "/tmp/work"
    assert payload["environment"]["cwd"] == "/tmp/work"

    # mounts, network and resource profile
    assert payload["environment"]["mounts"] == []
    assert payload["environment"]["network"] == "unrestricted"
    assert payload["environment"]["network_enforced"] is False
    assert payload["execution"]["resources"]["timeout_seconds"] == 30.0
    assert payload["execution"]["resources"]["max_output_bytes"] == environment.max_output_bytes

    # start/end/duration
    assert payload["execution"]["started_at"] == 1000.0
    assert payload["execution"]["ended_at"] == 1002.5
    assert payload["execution"]["duration_ms"] == 2500.0

    # exit code or structured error
    assert payload["execution"]["exit_code"] == 0
    assert payload["execution"]["error"] == ""

    # separately captured stdout/stderr, truncation, byte counts, digests
    assert payload["output"]["stdout"]["text"] == "all tests passed"
    assert payload["output"]["stderr"]["text"] == ""
    assert payload["output"]["stdout"]["truncated"] is False
    assert payload["output"]["stdout"]["original_bytes"] == 16
    assert len(payload["output"]["stdout"]["sha256"]) == 64

    # known effects, and whether that list is complete
    assert payload["effects"]["changed_paths"] == ["a.py"]
    assert payload["effects"]["bytes_written"] == 12
    assert payload["effects"]["complete"] is True


def test_stdout_and_stderr_stay_separate() -> None:
    """The neutral record never concatenates the streams.

    The first consumer renders them merged today. That concatenation belongs in
    its adapter: a record that has already merged them cannot be unmerged, so
    the neutral side keeps the distinction and the compat rendering diverges.
    """
    operation = make_operation()
    result = make_result(operation, stdout="OUT", stderr="ERR")
    payload = build_record(result, requested=operation).to_dict()

    assert payload["output"]["stdout"]["text"] == "OUT"
    assert payload["output"]["stderr"]["text"] == "ERR"
    assert payload["output"]["stdout"]["text"] != payload["output"]["stderr"]["text"]


def test_missing_normalization_is_recorded_as_missing() -> None:
    """An operation that failed before normalization says so, rather than faking it."""
    operation = make_operation(kind="nope.unknown")
    result = OperationResult(
        operation_id=operation.id,
        status=OperationStatus.FAILED,
        error="no handler registered for operation kind 'nope.unknown'",
    )
    payload = build_record(result, requested=operation).to_dict()

    assert payload["operation"]["normalized"] is None
    assert payload["operation"]["normalized_available"] is False


def test_preview_is_recorded_as_not_applied() -> None:
    """A preview is never recorded as work that happened."""
    operation = make_operation(apply=False)
    result = OperationResult(
        operation_id=operation.id,
        status=OperationStatus.PREVIEWED,
        rendering="preview: process.exec was not applied.",
        effects=Effects(complete=False),
    )
    payload = build_record(result, requested=operation).to_dict()

    assert payload["status"] == "previewed"
    assert payload["execution"]["applied"] is False
    assert payload["execution"]["previewed"] is True
    assert payload["effects"]["complete"] is False


def test_truncated_output_digest_is_scoped_to_what_is_stored() -> None:
    """The digest must not be mistaken for a digest of the original stream."""
    operation = make_operation()
    result = make_result(operation, stdout="head only", stdout_truncated=True, stdout_bytes=999_999)
    stdout = build_record(result, requested=operation).to_dict()["output"]["stdout"]

    assert stdout["truncated"] is True
    assert stdout["original_bytes"] == 999_999
    assert stdout["stored_bytes"] == len("head only")
    assert stdout["sha256_scope"] == "text-as-stored"


def test_unknown_byte_count_is_none_not_zero() -> None:
    """ "Nobody measured it" and "it was empty" are different facts."""
    operation = make_operation()
    result = make_result(operation, stdout="x", stdout_bytes=None)
    stdout = build_record(result, requested=operation).to_dict()["output"]["stdout"]

    assert stdout["original_bytes"] is None
    assert stdout["stored_bytes"] == 1


def test_caller_map_is_kept_whole() -> None:
    """Unrecognized provenance keys survive — shell-cli does not own these semantics."""
    operation = make_operation(caller={"agent": "colleague", "custom_field": "keep me"})
    payload = build_record(make_result(operation), requested=operation).to_dict()

    assert payload["caller"]["all"]["custom_field"] == "keep me"
    assert payload["caller"]["task_id"] is None


# --- 5. integrity -----------------------------------------------------------


def test_content_digest_is_reproducible_and_detects_tampering() -> None:
    operation = make_operation()
    record = build_record(make_result(operation), requested=operation, recorded_at=1234.5)
    payload = record.to_dict()

    recomputed = EvidenceRecord(body=record.body).to_dict()
    assert recomputed["integrity"]["content_sha256"] == payload["integrity"]["content_sha256"]

    tampered = dict(record.body)
    tampered["status"] = "succeeded-ish"
    changed = EvidenceRecord(body=tampered).to_dict()
    assert changed["integrity"]["content_sha256"] != payload["integrity"]["content_sha256"]


def test_record_is_json_serializable() -> None:
    operation = make_operation()
    record = build_record(make_result(operation), requested=operation)
    assert json.loads(record.to_json())["operation_id"] == operation.id


# --- 6. storage location and retention --------------------------------------


def test_store_is_anchored_to_the_source_root(environment: Environment) -> None:
    """Evidence lives in trusted control context, not in the writable work tree."""
    store = EvidenceStore.for_environment(environment)
    assert store.directory == Path(environment.source_root) / DEFAULT_STORE_SUBDIR
    assert not str(store.directory).startswith(str(environment.work_root))


def test_record_reports_whether_the_store_was_outside_the_work_root(
    environment: Environment,
) -> None:
    operation = make_operation()
    store = EvidenceStore.for_environment(environment)
    _, record = capture(
        make_result(operation), requested=operation, environment=environment, store=store
    )
    assert record.to_dict()["persistence"]["store_outside_work_root"] is True


def test_a_shared_root_deployment_is_reported_honestly(tmp_path: Path) -> None:
    """When both roots are the same tree, the separation is not real — say so."""
    shared = tmp_path / "repo"
    shared.mkdir()
    environment = Environment(source_root=shared, work_root=shared, runner=HostRunner())
    operation = make_operation()

    _, record = capture(
        make_result(operation),
        requested=operation,
        environment=environment,
        store=EvidenceStore.for_environment(environment),
    )
    assert record.to_dict()["persistence"]["store_outside_work_root"] is False


def test_records_round_trip_through_the_store(tmp_path: Path) -> None:
    store = EvidenceStore(directory=tmp_path / "evidence")
    operation = make_operation()
    capture(make_result(operation), requested=operation, store=store)

    records = store.records()
    assert len(records) == 1
    assert records[0]["operation_id"] == operation.id


def test_retention_prunes_by_count(tmp_path: Path) -> None:
    store = EvidenceStore(
        directory=tmp_path / "evidence",
        retention=RetentionPolicy(max_records=3, max_age_seconds=None),
    )
    for _ in range(6):
        operation = make_operation()
        capture(make_result(operation), requested=operation, store=store)

    assert len(store.paths()) == 3


def test_retention_prunes_by_age(tmp_path: Path) -> None:
    store = EvidenceStore(
        directory=tmp_path / "evidence",
        retention=RetentionPolicy(max_records=None, max_age_seconds=60.0),
    )
    operation = make_operation()
    capture(make_result(operation), requested=operation, store=store)
    stale = store.paths()[0]
    os.utime(stale, (0, 0))

    fresh = make_operation()
    capture(make_result(fresh), requested=fresh, store=store)

    remaining = store.paths()
    assert stale not in remaining
    assert len(remaining) == 1


def test_retention_defaults_are_bounded() -> None:
    """Neither bound is None by default — an unbounded store is a disk-fill bug."""
    policy = RetentionPolicy()
    assert policy.max_records is not None
    assert policy.max_age_seconds is not None


def test_retention_declares_that_it_only_runs_on_write() -> None:
    """The limitation is a field, not just a docstring."""
    assert RetentionPolicy().to_dict()["enforced_on_write_only"] is True


def test_an_idle_store_prunes_nothing(tmp_path: Path) -> None:
    """Nothing visits an idle store. Pinned so the docstring stays true."""
    store = EvidenceStore(
        directory=tmp_path / "evidence",
        retention=RetentionPolicy(max_records=None, max_age_seconds=1.0),
    )
    operation = make_operation()
    capture(make_result(operation), requested=operation, store=store)
    os.utime(store.paths()[0], (0, 0))

    assert len(store.paths()) == 1, "no write happened, so no pruning happened"


def test_a_corrupt_record_does_not_break_the_audit_trail(tmp_path: Path) -> None:
    store = EvidenceStore(directory=tmp_path / "evidence")
    operation = make_operation()
    capture(make_result(operation), requested=operation, store=store)
    (store.directory / "0-corrupt.json").write_text("{not json", encoding="utf-8")

    records = store.records()
    assert len(records) == 1
    assert records[0]["operation_id"] == operation.id


def test_an_absent_store_reads_as_empty(tmp_path: Path) -> None:
    store = EvidenceStore(directory=tmp_path / "never-created")
    assert store.paths() == []
    assert store.records() == []


def test_filenames_sort_chronologically(tmp_path: Path) -> None:
    store = EvidenceStore(directory=tmp_path / "evidence")
    for moment in (300.0, 100.0, 200.0):
        operation = make_operation()
        record = build_record(make_result(operation), requested=operation, recorded_at=moment)
        store.write(record)

    moments = [json.loads(p.read_text(encoding="utf-8"))["recorded_at"] for p in store.paths()]
    assert moments == sorted(moments)


# --- 7. the module stays inside the constraints ------------------------------


def test_evidence_module_imports_only_stdlib_and_self() -> None:
    """The zero-dependency posture, checked for this module specifically.

    tests/test_zero_deps.py enumerates core modules explicitly rather than
    globbing, so a new module is only covered there once someone adds it. This
    keeps the guard true for shell.evidence regardless.
    """
    import ast
    import sys

    source = Path(__file__).resolve().parents[1] / "shell" / "evidence.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    offenders = sorted(
        name for name in names if name not in sys.stdlib_module_names and name != "shell"
    )
    assert not offenders, f"shell/evidence.py imports non-stdlib modules: {offenders}"
