"""Tests for ``fs.list`` (``shell/fs/list.py``).

Mirrors ``tests/test_fs_read.py``'s structure since both handlers share the
same confinement (``_safe_path``) and truncation (``_truncate``) semantics,
each re-ported independently per module rather than factored into a shared
helper -- see ``shell/fs/list.py``'s module docstring for why.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

import pytest

from shell import operations
from shell.environment import DEFAULT_MAX_OUTPUT_BYTES, Environment, WorkspaceKind
from shell.evidence import EvidenceRecord
from shell.fs import list as fs_list
from shell.operations import ExecutionProfile, Operation, OperationIntent
from shell.results import SCHEMA_VERSION, OperationResult, OperationStatus
from shell.runners.host import HostRunner

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "colleague" / "behavior.json"


def _load_fixture(name: str) -> dict:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["behaviors"][name]


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
    )


def _list(env: Environment, arguments: dict, **kwargs) -> OperationResult:
    operation = Operation(kind=fs_list.KIND, arguments=arguments, **kwargs)
    return operations.execute(operation, env)


def _sink() -> tuple[list[EvidenceRecord], Callable[[EvidenceRecord], None]]:
    records: list[EvidenceRecord] = []
    return records, records.append


# --- registration -------------------------------------------------------


def test_fs_list_self_registers_on_import() -> None:
    assert fs_list.KIND in operations.registered_kinds()


def test_fs_list_is_an_observe_intent_operation() -> None:
    normalized = operations.normalize(Operation(kind=fs_list.KIND, arguments={}))
    assert normalized.intent is OperationIntent.OBSERVE
    assert normalized.profile is ExecutionProfile.OBSERVE
    assert normalized.requires_apply is False


# --- basic shape: sorted, trailing slash on directories --------------------


def test_list_entries_are_sorted_with_trailing_slash_on_directories(env: Environment) -> None:
    (env.work_root / "b.txt").write_text("x", encoding="utf-8")
    (env.work_root / "a.txt").write_text("x", encoding="utf-8")
    (env.work_root / "sub").mkdir()

    result = _list(env, {"path": "."})

    assert result.succeeded
    assert result.rendering == "a.txt\nb.txt\nsub/"
    assert result.output["entries"] == ["a.txt", "b.txt", "sub/"]
    assert result.output["truncated"] is False
    assert result.effects.complete is True
    assert result.effects.changed_paths == ()
    assert result.schema_version == SCHEMA_VERSION


def test_list_matches_colleagues_captured_fixture(env: Environment) -> None:
    """Byte-for-byte against colleague's own captured ``list_dir`` shape
    (pinned commit 28fee290c51fc4310b9fc576981809ad5c3132c6)."""
    (env.work_root / "dirtest").mkdir()
    (env.work_root / "dirtest" / "b.txt").write_text("x", encoding="utf-8")
    (env.work_root / "dirtest" / "a.txt").write_text("x", encoding="utf-8")
    (env.work_root / "dirtest" / "sub").mkdir()

    fixture = _load_fixture("list_dir_shape")
    result = _list(env, {"path": "dirtest"})

    assert result.succeeded
    assert result.rendering == fixture["result"]


def test_path_defaults_to_the_work_root_itself_when_omitted(env: Environment) -> None:
    (env.work_root / "only.txt").write_text("x", encoding="utf-8")

    result = _list(env, {})

    assert result.succeeded
    assert result.output["path"] == "."
    assert result.rendering == "only.txt"


# --- recoverable, model-visible errors -------------------------------------


def test_listing_a_file_is_a_recoverable_failed_result(env: Environment) -> None:
    (env.work_root / "e.txt").write_text("x", encoding="utf-8")

    fixture = _load_fixture("list_dir_not_a_directory")
    result = _list(env, {"path": "e.txt"})

    assert result.status is OperationStatus.FAILED
    assert result.error == fixture["error"]
    assert "not a directory" in result.error


def test_listing_a_nonexistent_path_gives_the_same_not_a_directory_message(
    env: Environment,
) -> None:
    """``Path.is_dir()`` answers False for both "is a file" and "does not
    exist" -- colleague's ``_list_dir`` never distinguishes the two, and
    neither does this port (see the module docstring)."""
    result = _list(env, {"path": "does/not/exist"})

    assert result.status is OperationStatus.FAILED
    assert result.error == "not a directory: does/not/exist"


def test_dot_dot_path_escape_is_refused(env: Environment) -> None:
    outside = env.work_root.parent / "outside_listing"
    outside.mkdir()

    result = _list(env, {"path": "../outside_listing"})

    assert result.status is OperationStatus.FAILED
    assert "escapes the work root" in result.error


def test_symlinked_directory_inside_root_pointing_outside_it_is_refused(
    env: Environment,
) -> None:
    """The interesting confinement case for ``fs.list``: a symlink whose
    literal name never mentions anything outside the root, but whose
    resolved target does. A confinement check on the literal string alone
    would miss this."""
    outside_dir = env.work_root.parent / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("x", encoding="utf-8")

    link_dir = env.work_root / "linked_dir"
    os.symlink(outside_dir, link_dir, target_is_directory=True)

    result = _list(env, {"path": "linked_dir"})

    assert result.status is OperationStatus.FAILED
    assert "escapes the work root" in result.error


def test_a_symlinked_directory_that_stays_inside_the_root_is_listable(env: Environment) -> None:
    real_dir = env.work_root / "real_dir"
    real_dir.mkdir()
    (real_dir / "inside.txt").write_text("x", encoding="utf-8")
    link_dir = env.work_root / "alias_dir"
    os.symlink(real_dir, link_dir, target_is_directory=True)

    result = _list(env, {"path": "alias_dir"})

    assert result.succeeded
    assert result.rendering == "inside.txt"


# --- a handler crash on malformed input: recoverable, never a run abort ---


def test_a_nul_byte_in_the_path_crashes_the_handler_but_is_recovered() -> None:
    """Same NUL-byte crash case as ``fs.read`` (colleague's
    ``execute_wraps_non_tool_error`` characterization), replayed against
    ``fs.list``'s own ``_safe_path`` -- the wrap in ``operations.execute`` is
    generic to any handler, not special-cased to ``fs.read``."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source"
        work = Path(tmp) / "work"
        source.mkdir()
        work.mkdir()
        env = Environment(
            source_root=source,
            work_root=work,
            runner=HostRunner(),
            workspace=WorkspaceKind.WORKTREE,
        )

        records, sink = _sink()
        operation = Operation(kind=fs_list.KIND, arguments={"path": "a\x00b"})

        result = operations.execute(operation, env, evidence_sink=sink)

    assert result.status is OperationStatus.FAILED
    assert "ValueError" in result.error
    assert fs_list.KIND in result.error

    execution = records[0].to_dict()["execution"]
    assert execution["handler_entered"] is True
    assert execution["handler_disposition"] == "crashed"
    assert execution["applied"] is None


def test_the_raw_handler_call_is_not_wrapped_without_execute(env: Environment) -> None:
    operation = Operation(kind=fs_list.KIND, arguments={"path": "a\x00b"})

    with pytest.raises(ValueError):
        fs_list._list(operation, env)


# --- fs.* is not gated by the run_command policy ---------------------------


def test_fs_list_is_ungated_regardless_of_policy(env: Environment) -> None:
    from shell.policy import Policy

    deny_everything = Policy(
        run_command={"deny": ["anything"]},
        present=frozenset({"run_command"}),
    )

    result = _list(env, {})
    gated_result = operations.execute(
        Operation(kind=fs_list.KIND, arguments={}), env, policy=deny_everything
    )

    assert gated_result.status is OperationStatus.SUCCEEDED
    assert gated_result.verdict.decision.value == "ungated"
    assert gated_result.rendering == result.rendering


# --- resource limit override -----------------------------------------------


def test_max_output_bytes_override_shrinks_the_truncation_window(env: Environment) -> None:
    for name in ("aa.txt", "bb.txt", "cc.txt", "dd.txt"):
        (env.work_root / name).write_text("x", encoding="utf-8")

    result = _list(env, {"path": "."}, max_output_bytes=5)

    assert result.output["truncated"] is True
    assert "truncated at 5 chars" in result.rendering


def test_default_max_output_bytes_matches_environment_default(env: Environment) -> None:
    assert DEFAULT_MAX_OUTPUT_BYTES == 25_000
