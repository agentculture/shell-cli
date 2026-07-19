"""Tests for ``process.shell`` (``shell/process/shell.py``).

Two of these tests are the point of the whole slice, and neither is synthetic:

* ``test_a_real_command_writes_a_file_it_never_declared`` runs an actual shell
  command that creates a file nobody told the operation about, and pins that the
  result reports ``effects_complete = False`` with an empty changed-path list.
  The empty list means "not observed", never "nothing happened".
* ``test_a_real_command_echoing_a_declared_secret_is_redacted_in_the_record``
  runs an actual process that prints a declared secret, and pins that the
  evidence record comes back with it removed. The redaction machinery was built
  against synthetic results; this is where it meets a live process.

Both handler modules are imported at module scope. Handlers register on import,
so a profile-distinction test that relied on another test file having imported
``process.exec`` first would pass serially and fail under ``pytest -n auto``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from shell import operations
from shell.environment import DEFAULT_MAX_OUTPUT_BYTES, Environment, WorkspaceKind
from shell.evidence import REDACTED, EvidenceRecord
from shell.fs import write as fs_write
from shell.operations import ExecutionProfile, Operation, OperationIntent
from shell.policy import load_policy
from shell.process import exec as process_exec
from shell.process import shell as process_shell
from shell.results import OperationResult, OperationStatus, PolicyDecision, SecretHandling
from shell.runners.host import HostRunner
from tests.test_honesty import overclaims

PYTHON = sys.executable

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "colleague" / "behavior.json"

#: The first consumer's own output cap (``_DEFAULT_MAX_OUTPUT_CHARS``, inlined at
#: ``colleague/tools.py:675``). The committed fixtures were captured under it.
_COLLEAGUE_MAX_OUTPUT_CHARS = 25_000


@pytest.fixture
def env(tmp_path: Path) -> Environment:
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


def run(env: Environment, command: str, **kwargs: Any) -> OperationResult:
    kwargs.setdefault("apply", True)
    return operations.execute(
        Operation(kind=process_shell.KIND, arguments={"command": command}, **kwargs), env
    )


def _fixture(name: str) -> dict[str, Any]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["behaviors"][name]


def compat_render(result: OperationResult, limit: int = _COLLEAGUE_MAX_OUTPUT_CHARS) -> str:
    """Compose the first consumer's legacy string from the neutral result.

    This is the adapter's job, written here as a test helper to prove it is
    *possible* from what the neutral result carries. It reproduces
    ``colleague/tools.py:1047-1048`` — ``f"exit={code}\\n{stdout}{stderr}"``,
    then ``_truncate`` over the composed body, so the ``"exit=0\\n"`` prefix
    counts toward the cap exactly as it does there.

    Nothing in ``shell/`` does this. The concatenation is lossy and belongs on
    the far side of the seam.
    """
    evidence = result.evidence
    body = f"exit={evidence.exit_code}\n{evidence.stdout}{evidence.stderr}"
    if len(body) <= limit:
        return body
    return body[:limit] + f"\n... [truncated at {limit} chars]"


# --- registration -----------------------------------------------------------


def test_registers_as_a_project_profile_execute_operation() -> None:
    spec = operations.handler_for(process_shell.KIND)
    assert spec.intent is OperationIntent.EXECUTE
    assert spec.default_profile is ExecutionProfile.PROJECT
    assert process_shell.KIND in operations.registered_kinds()


def test_previews_without_apply_and_starts_nothing(env: Environment) -> None:
    result = run(env, "echo x > previewed.txt", apply=False)
    assert result.status is OperationStatus.PREVIEWED
    assert not bool(result)
    assert not (env.work_root / "previewed.txt").exists()


# --- REAL integration: undeclared effects ----------------------------------


def test_a_real_command_writes_a_file_it_never_declared(env: Environment) -> None:
    """Acceptance 3, run for real rather than asserted about a fabricated result.

    The command creates a file the operation's arguments never mention. Nothing
    at this layer can enumerate that, so the effect list is empty AND marked
    incomplete — the two together are the honest statement. An empty list marked
    complete would be a lie about a file that is sitting on disk.
    """
    sneaky = env.work_root / "sneaky.txt"
    result = run(env, "echo undeclared > sneaky.txt")

    assert result.status is OperationStatus.SUCCEEDED
    assert sneaky.read_text(encoding="utf-8").strip() == "undeclared"

    assert result.effects.changed_paths == ()
    assert result.effects.bytes_written == 0
    assert result.effects_complete is False
    assert result.to_dict()["effects"]["complete"] is False


def test_fs_write_claims_completeness_where_a_process_cannot(env: Environment) -> None:
    """The contrast that makes the incomplete marker mean something.

    ``fs.write`` performed its one mutation itself and can name it, so
    ``complete=True`` is earned there. A process cannot make that claim about
    itself, and this pins that the two do not report the same thing.
    """
    written = operations.execute(
        Operation(
            kind=fs_write.WRITE_KIND,
            arguments={"path": "declared.txt", "content": "hello"},
            apply=True,
        ),
        env,
    )
    assert written.effects_complete is True
    assert written.effects.changed_paths == ("declared.txt",)

    ran = run(env, "echo undeclared > also.txt")
    assert ran.effects_complete is False
    assert ran.effects.changed_paths == ()


def test_a_real_command_leaves_the_work_root(env: Environment) -> None:
    """The guard-not-a-sandbox posture, demonstrated rather than asserted.

    ``fs.write`` refuses a path outside the work root. A shell command reaches
    the same place with an absolute path and no refusal happens, because there
    is nothing here that could refuse it. The result says so in its own
    metadata.
    """
    escaped = env.source_root / "escaped.txt"
    result = run(env, f"echo out-of-root > {escaped}")

    assert result.status is OperationStatus.SUCCEEDED
    assert escaped.exists(), "the command left the work root, which is the point"
    assert result.output["confinement"]["path_confined"] is False


# --- REAL integration: redaction meets a live process -----------------------


def test_a_real_command_echoing_a_declared_secret_is_redacted_in_the_record(
    env: Environment,
) -> None:
    """Acceptance 4: a live process prints a declared secret; nothing keeps it.

    Redaction covers BOTH representations. The record is scrubbed when it is
    built, and the live ``OperationResult`` is scrubbed before it is returned.
    An earlier version of this test pinned the opposite for the result — it
    asserted ``result.evidence.stdout == secret`` and described that as a known
    limit. It was a real leak rather than a limit: the first consumer renders
    result output back to the model, so the unscrubbed result was the exact path
    a declared secret would have travelled. Scrubbing only the record protected
    the audit trail and not the reader.

    What stays true is the *undeclared* gap, pinned by
    ``test_an_undeclared_secret_is_not_detected`` below.
    """
    secret = "s3cr3t-token-value-do-not-record"  # nosec B105 - test fixture, not a credential
    records: list[EvidenceRecord] = []

    result = operations.execute(
        Operation(
            kind=process_shell.KIND,
            arguments={"command": f"printf '%s' '{secret}'; printf 'x%sx' '{secret}' >&2"},
            apply=True,
        ),
        env,
        secrets={"API_TOKEN": secret},
        evidence_sink=records.append,
    )

    # The process really did emit it — otherwise the redaction below is vacuous.
    # The record's replacement count proves that: it counts what was actually
    # found and removed, so a command that emitted nothing could not reach 3.
    assert result.status is OperationStatus.SUCCEEDED

    # The live result the caller holds is scrubbed too, not only the record.
    assert result.evidence.stdout == REDACTED
    assert result.evidence.stderr == f"x{REDACTED}x"
    assert secret not in json.dumps(result.to_dict(), default=str)
    assert result.evidence.secret_handling is SecretHandling.REDACTED
    assert result.evidence.redaction_complete is False

    body = records[0].body
    serialized = json.dumps(body, default=str)
    assert secret not in serialized, "a declared secret must not survive anywhere in the record"

    assert body["output"]["stdout"]["text"] == REDACTED
    assert body["output"]["stderr"]["text"] == f"x{REDACTED}x"
    # The command string itself contained the secret, and the record keeps the
    # requested operation — so redaction has to reach the arguments too.
    assert secret not in json.dumps(body["operation"], default=str)

    redaction = body["redaction"]
    assert redaction["secret_names"] == ["API_TOKEN"]
    assert redaction["replacements"] >= 3
    assert redaction["complete"] is False, "redaction is never claimed complete"


def test_an_undeclared_secret_is_not_detected(env: Environment) -> None:
    """The other half of the honest claim: only *declared* values are removed."""
    records: list[EvidenceRecord] = []
    operations.execute(
        Operation(
            kind=process_shell.KIND,
            arguments={"command": "printf '%s' 'undeclared-credential'"},
            apply=True,
        ),
        env,
        secrets={"API_TOKEN": "something-else"},
        evidence_sink=records.append,
    )
    assert records[0].body["output"]["stdout"]["text"] == "undeclared-credential"
    assert records[0].body["redaction"]["complete"] is False


# --- profiles: the distinction acceptance 2 asks for ------------------------


def test_a_control_profile_is_refused_for_a_raw_string(env: Environment) -> None:
    """Acceptance 5: a raw string may not acquire control-plane trust."""
    result = run(env, "git status", profile=ExecutionProfile.CONTROL)
    assert result.status is OperationStatus.FAILED
    assert "may not run under the 'control' profile" in result.error
    assert "process.exec" in result.error


def test_the_observe_profile_is_refused_for_a_raw_string(env: Environment) -> None:
    result = run(env, "echo hi", profile=ExecutionProfile.OBSERVE)
    assert result.status is OperationStatus.FAILED
    assert "may not run under the 'observe' profile" in result.error


def test_shell_and_control_execution_carry_distinct_profiles_in_evidence(
    env: Environment,
) -> None:
    """Acceptance 2: two trust stories, two profiles, on two different kinds.

    A model-authored string and a control-plane invocation (a hook, a git call,
    a capability CLI) are separable in the record on ``operation.kind`` and
    ``operation.normalized.profile`` alone, with no cross-referencing.
    """
    records: list[EvidenceRecord] = []

    operations.execute(
        Operation(
            kind=process_shell.KIND,
            arguments={"command": "echo model-authored"},
            apply=True,
            caller={"agent": "colleague", "tool": "run_command"},
        ),
        env,
        evidence_sink=records.append,
    )
    operations.execute(
        Operation(
            kind=process_exec.KIND,
            arguments={"argv": [PYTHON, "-c", "pass"]},
            profile=ExecutionProfile.CONTROL,
            apply=True,
            caller={"agent": "colleague", "tool": "pre_tool_hook"},
        ),
        env,
        evidence_sink=records.append,
    )

    shell_record, control_record = (r.body for r in records)

    assert shell_record["operation"]["kind"] == "process.shell"
    assert shell_record["operation"]["normalized"]["profile"] == "project"

    assert control_record["operation"]["kind"] == "process.exec"
    assert control_record["operation"]["normalized"]["profile"] == "control"

    assert (
        shell_record["operation"]["normalized"]["profile"]
        != control_record["operation"]["normalized"]["profile"]
    )


def test_a_shell_operation_can_never_report_a_non_project_profile(env: Environment) -> None:
    """There is no argument shape that gets a raw string past the profile check."""
    for profile in (ExecutionProfile.CONTROL, ExecutionProfile.OBSERVE):
        result = run(env, "echo hi", profile=profile)
        assert result.status is OperationStatus.FAILED
        assert result.evidence.exit_code is None


# --- non-confinement is stated in the result, not only in the docs ----------


def test_the_result_states_its_own_non_confinement(env: Environment) -> None:
    """Acceptance: result METADATA must carry the posture, not just prose."""
    result = run(env, "echo hi")
    confinement = result.output["confinement"]

    assert confinement["path_confined"] is False
    assert confinement["uses_shell"] is True
    assert confinement["gate_inspects_reinterpreted_string"] is True

    note = confinement["note"].lower()
    assert "not path-confined" in note
    assert "starting directory" in note
    assert "never an adversarial one" in note
    assert overclaims(confinement["note"]) == []

    # Present in the JSON form too — a consumer reading only ``to_dict`` gets it.
    assert result.to_dict()["output"]["confinement"]["path_confined"] is False


def test_the_argv_kind_states_a_different_and_narrower_claim(env: Environment) -> None:
    """``process.exec`` removes re-interpretation — and nothing else.

    Its confinement block must not read as "safer therefore confined": the
    ``path_confined`` answer is identical, and only the shell-specific fields
    differ.
    """
    argv_result = operations.execute(
        Operation(kind=process_exec.KIND, arguments={"argv": [PYTHON, "-c", "pass"]}, apply=True),
        env,
    )
    confinement = argv_result.output["confinement"]

    assert confinement["path_confined"] is False
    assert confinement["uses_shell"] is False
    assert confinement["gate_inspects_reinterpreted_string"] is False
    assert "not path-confined" in confinement["note"].lower()
    assert overclaims(confinement["note"]) == []


def test_the_runner_posture_rides_on_every_process_result(env: Environment) -> None:
    result = run(env, "echo hi")
    evidence = result.to_dict()["evidence"]
    assert evidence["isolation"] == "none"
    assert "not a sandbox" in evidence["isolation_note"].lower()


# --- run_command parity -----------------------------------------------------


def test_each_invocation_gets_a_fresh_shell(env: Environment) -> None:
    """Parity: no state survives between commands."""
    first = run(env, "FOO=set-by-first; export FOO; echo done")
    assert first.status is OperationStatus.SUCCEEDED
    second = run(env, 'echo "${FOO-unset}"')
    assert second.evidence.stdout.strip() == "unset"


def test_cwd_is_rooted_at_the_work_root(env: Environment) -> None:
    result = run(env, "pwd")
    assert Path(result.evidence.stdout.strip()).resolve() == env.work_root


def test_the_timeout_is_bounded_and_reported_as_its_own_status(env: Environment) -> None:
    result = run(env, "sleep 30", timeout_seconds=0.3)
    assert result.status is OperationStatus.TIMED_OUT
    assert result.output["timed_out"] is True
    assert result.output["termination"]["reason"] == "timeout"
    assert "exceeded its timeout" in result.error


def test_the_default_output_bound_matches_the_first_consumers_cap(env: Environment) -> None:
    assert env.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES == _COLLEAGUE_MAX_OUTPUT_CHARS


@pytest.mark.parametrize(
    "fixture_name, offset",
    [
        ("run_command_truncation_boundary_not_truncated", 0),
        ("run_command_truncation_boundary_truncated", 1),
    ],
)
def test_the_adapter_can_reproduce_the_legacy_truncation_boundary(
    env: Environment, fixture_name: str, offset: int
) -> None:
    """Parity: the legacy string is recoverable from the neutral result, exactly.

    The neutral result keeps the streams apart; composing them is the adapter's
    job. This runs the same command the baseline capture ran against the real
    colleague executor and checks the composition byte-for-byte against the
    committed fixture — including the ``"exit=0\\n"`` prefix counting toward the
    25000-char cap.
    """
    boundary = _COLLEAGUE_MAX_OUTPUT_CHARS - len("exit=0\n") + offset
    result = run(env, f"{PYTHON} -c \"import sys; sys.stdout.write('a'*{boundary})\"")

    assert result.status is OperationStatus.SUCCEEDED
    assert compat_render(result) == _fixture(fixture_name)["result"]


def test_the_adapter_can_reproduce_the_legacy_exit_and_body_shape(env: Environment) -> None:
    """Parity: ``f"exit={code}\\n{stdout}{stderr}"``, from separate captures.

    The fixture was produced by colleague's real ``_run_command`` at the pinned
    SHA and records ``ok: true`` for ``exit=3`` — which is why a non-zero exit
    is a SUCCEEDED operation here rather than a failed one.
    """
    fixture = _fixture("run_command_exit_code_and_body_shape")
    command = (
        f"{PYTHON} -c \"import sys; print('out-line'); "
        "print('err-line', file=sys.stderr); sys.exit(3)\""
    )
    result = run(env, command)

    assert fixture["ok"] is True
    assert result.status is OperationStatus.SUCCEEDED
    assert result.evidence.stdout == "out-line\n"
    assert result.evidence.stderr == "err-line\n"
    assert compat_render(result) == fixture["result"]


# --- honest reporting of what could not be captured -------------------------


def test_output_captured_past_a_surviving_child_is_marked_a_prefix(tmp_path: Path) -> None:
    """A descendant holding the pipe open is reported, never flattened.

    The command exits promptly but leaves a background child holding the write
    end of stdout, so the readers never see EOF. The runner gives up on its
    drain bound and the result must say the streams are a prefix — in the
    output payload AND on the standard "was this record any good?" field.
    """
    environment = Environment(
        source_root=tmp_path,
        work_root=tmp_path,
        runner=HostRunner(drain_grace_seconds=0.2),
        env_passthrough=("PATH",),
    )
    result = operations.execute(
        Operation(
            kind=process_shell.KIND,
            arguments={"command": "sleep 2 & printf 'partial'; exit 0"},
            apply=True,
        ),
        environment,
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.output["output_complete"] is False
    assert result.evidence.degraded is True
    assert "prefix" in result.evidence.degraded_reason
    assert "captured output is a prefix" in result.rendering


# --- argument errors are recoverable steps ----------------------------------


@pytest.mark.parametrize("arguments", [{}, {"command": ""}, {"command": "   "}, {"command": 7}])
def test_a_malformed_command_is_a_failed_result_not_an_exception(
    env: Environment, arguments: dict[str, Any]
) -> None:
    result = operations.execute(
        Operation(kind=process_shell.KIND, arguments=arguments, apply=True), env
    )
    assert result.status is OperationStatus.FAILED
    assert "requires 'command'" in result.error


def test_an_argv_vector_is_refused_by_name(env: Environment) -> None:
    result = operations.execute(
        Operation(kind=process_shell.KIND, arguments={"argv": ["echo", "hi"]}, apply=True), env
    )
    assert result.status is OperationStatus.FAILED
    assert "not an argv vector" in result.error
    assert "process.exec" in result.error


# --- policy ------------------------------------------------------------------


def test_the_run_command_policy_gates_process_shell(env: Environment) -> None:
    result = operations.execute(
        Operation(kind=process_shell.KIND, arguments={"command": "rm -rf /"}, apply=True),
        env,
        policy=load_policy(data={"run_command": {"allow": [], "deny": ["rm"]}}),
    )
    assert result.status is OperationStatus.DENIED
    assert result.verdict.decision is PolicyDecision.DENIED
    assert result.evidence.exit_code is None, "a denied command must never have run"


def test_the_gate_sees_the_rewritten_command(env: Environment) -> None:
    """A rewrite cannot turn a denied command into an allowed one.

    Pinned again here because ``process.shell`` is the kind the hole would
    actually be exploited through.
    """
    result = operations.execute(
        Operation(kind=process_shell.KIND, arguments={"command": "echo safe"}, apply=True),
        env,
        policy=load_policy(data={"run_command": {"allow": [], "deny": ["rm"]}}),
        rewrite=lambda op: {"command": "rm -rf /"},
    )
    assert result.status is OperationStatus.DENIED
