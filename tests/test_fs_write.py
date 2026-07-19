"""Tests for ``fs.write`` and ``fs.edit`` (``shell/fs/write.py``).

The single highest-stakes property here is ``bytes_written`` semantics:
``fs.write`` accounts the FULL content, ``fs.edit`` accounts ONLY the
replacement bytes. This feeds a downstream consumer's ROI accounting, so the
tests that pin it are driven directly from the committed colleague
characterization fixture (``tests/fixtures/colleague/behavior.json``,
captured against colleague at pinned SHA ``28fee29``) rather than from
locally-invented numbers — an off-by-semantics bug here would otherwise be
invisible to a reviewer comparing this file against itself.

Also covered: the two independent confinement mechanisms (root escape,
including a symlink escape, versus the ``check_write`` read-only-subtree
refusal this task wires up), preview-before-apply semantics, and that the
lifecycle pipeline needs nothing beyond ``Operation``/``Environment`` to run
these handlers — no ``spawn``, ``batch_spawn``, ``deepthink`` or allowlist.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import shell.fs.write as fs_write
from shell import operations
from shell.environment import Environment, WorkspaceKind
from shell.operations import Operation, OperationIntent
from shell.results import Effects, OperationStatus, PolicyDecision
from shell.runners.host import HostRunner

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BEHAVIOR = json.loads(
    (_REPO_ROOT / "tests" / "fixtures" / "colleague" / "behavior.json").read_text(encoding="utf-8")
)["behaviors"]


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    source = tmp_path / "checkout"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        workspace=WorkspaceKind.WORKTREE,
    )


def _write(path: str, content: str, *, apply: bool = True) -> Operation:
    return Operation(
        kind=fs_write.WRITE_KIND, arguments={"path": path, "content": content}, apply=apply
    )


def _edit(
    path: str, old: str, new: str, *, replace_all: bool = False, apply: bool = True
) -> Operation:
    arguments = {"path": path, "old_string": old, "new_string": new}
    if replace_all:
        arguments["replace_all"] = True
    return Operation(kind=fs_write.EDIT_KIND, arguments=arguments, apply=apply)


# --- registration -------------------------------------------------------


def test_both_kinds_are_registered_as_mutations() -> None:
    assert fs_write.WRITE_KIND in operations.registered_kinds()
    assert fs_write.EDIT_KIND in operations.registered_kinds()
    normalized_write = operations.normalize(Operation(kind=fs_write.WRITE_KIND))
    normalized_edit = operations.normalize(Operation(kind=fs_write.EDIT_KIND))
    assert normalized_write.intent is OperationIntent.MUTATE
    assert normalized_edit.intent is OperationIntent.MUTATE


def test_fs_package_still_predeclares_no_exports() -> None:
    """r8: shell/fs/__init__.py stays empty even after shell.fs.write is imported.

    Importing shell.fs.write necessarily binds ``write`` onto ``shell.fs`` as
    an ordinary Python submodule attribute -- that is unavoidable import
    machinery, not a curated export from ``__init__.py`` itself, whose own
    ``__all__`` is what r8 actually constrains. The stricter, general version
    of this check (no *non-module* attribute predeclared) lives in
    ``tests/test_operations.py::test_handler_packages_predeclare_no_exports``.
    """
    import shell.fs

    assert shell.fs.__all__ == ()


def test_execute_needs_no_spawn_batch_spawn_deepthink_or_allowlist() -> None:
    """The executor serves these primitives without colleague's injected callables."""
    names = set(inspect.signature(operations.execute).parameters)
    assert names == {
        "operation",
        "environment",
        "policy",
        "rewrite",
        "evidence_store",
        "evidence_sink",
        "secrets",
        "reveal_secrets_in_result",
    }
    for forbidden in ("spawn", "batch_spawn", "deepthink", "allowlist"):
        assert forbidden not in names


# --- bytes_written: driven directly from the pinned colleague fixture ---


def test_write_bytes_written_matches_the_colleague_fixture(env: Environment) -> None:
    fixture = _BEHAVIOR["write_file_bytes_written"]
    result = operations.execute(_write("w.txt", "hello world"), env)

    assert result.status is OperationStatus.SUCCEEDED
    assert result.effects.bytes_written == fixture["bytes_written"] == 11
    assert result.rendering == fixture["result"] == "wrote 11 bytes to w.txt"
    assert result.effects.changed_paths == tuple(fixture["changed"])
    assert (env.work_root / "w.txt").read_text(encoding="utf-8") == "hello world"


def test_write_creates_missing_parent_directories_like_the_fixture(env: Environment) -> None:
    fixture = _BEHAVIOR["write_file_creates_nested_dirs"]
    result = operations.execute(_write("nested/deep/file.txt", "hi"), env)

    assert result.status is OperationStatus.SUCCEEDED
    assert result.output["path"] == fixture["changed_file"] == "nested/deep/file.txt"
    assert result.effects.bytes_written == 2
    assert (env.work_root / "nested" / "deep" / "file.txt").read_text(encoding="utf-8") == "hi"


def test_edit_bytes_written_is_replacement_only_matching_the_fixture(env: Environment) -> None:
    """'AAAA BBBB CCCC' (14 bytes) has 'BBBB' replaced with 'XY' (2 bytes) --
    bytes_written must be 2, never the 14-byte file, exactly as colleague's
    fixture pins it."""
    fixture = _BEHAVIOR["edit_file_bytes_written_single"]
    (env.work_root / "e.txt").write_text("AAAA BBBB CCCC", encoding="utf-8")

    result = operations.execute(_edit("e.txt", "BBBB", "XY"), env)

    assert result.status is OperationStatus.SUCCEEDED
    assert result.effects.bytes_written == fixture["bytes_written"] == 2
    assert result.effects.bytes_written < len("AAAA BBBB CCCC".encode("utf-8"))
    assert result.rendering == fixture["result"]
    assert (env.work_root / "e.txt").read_text(encoding="utf-8") == "AAAA XY CCCC"


def test_edit_replace_all_bytes_written_scales_with_occurrences(env: Environment) -> None:
    fixture = _BEHAVIOR["edit_file_bytes_written_replace_all"]
    (env.work_root / "r.txt").write_text("XX YY XX YY XX", encoding="utf-8")

    result = operations.execute(_edit("r.txt", "XX", "Q", replace_all=True), env)

    assert result.status is OperationStatus.SUCCEEDED
    assert result.effects.bytes_written == fixture["bytes_written"] == 3
    assert result.rendering == fixture["result"] == "edited r.txt: replaced 3 occurrences"
    assert (env.work_root / "r.txt").read_text(encoding="utf-8") == "Q YY Q YY Q"


def test_edit_old_string_not_found_matches_the_fixture_error(env: Environment) -> None:
    fixture = _BEHAVIOR["edit_file_old_string_not_found"]
    (env.work_root / "e.txt").write_text("AAAA BBBB CCCC", encoding="utf-8")

    result = operations.execute(_edit("e.txt", "nope", "x"), env)

    assert result.status is OperationStatus.FAILED
    assert "not found" in result.error
    assert result.error == fixture["error"]


def test_edit_ambiguous_without_replace_all_matches_the_fixture_error(env: Environment) -> None:
    fixture = _BEHAVIOR["edit_file_ambiguous_without_replace_all"]
    (env.work_root / "amb.txt").write_text("XX YY XX", encoding="utf-8")

    result = operations.execute(_edit("amb.txt", "XX", "Z"), env)

    assert result.status is OperationStatus.FAILED
    assert "not unique" in result.error
    assert result.error == fixture["error"]


# --- results carry schema_version and declared changed-path effects -----


def test_write_result_carries_schema_version(env: Environment) -> None:
    result = operations.execute(_write("v.txt", "x"), env)
    assert result.schema_version == "0"
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["schema_version"] == "0"


def test_write_effects_are_declared_complete(env: Environment) -> None:
    """A single fs.write handler genuinely knows its one effect."""
    result = operations.execute(_write("c.txt", "content"), env)
    assert result.effects == Effects(changed_paths=("c.txt",), bytes_written=7, complete=True)


def test_edit_effects_are_declared_complete(env: Environment) -> None:
    (env.work_root / "d.txt").write_text("aaa", encoding="utf-8")
    result = operations.execute(_edit("d.txt", "aaa", "bb"), env)
    assert result.effects.changed_paths == ("d.txt",)
    assert result.effects.bytes_written == 2
    assert result.effects.complete is True


# --- preview semantics: a preview is never a success ---------------------


def test_write_previews_by_default_and_touches_nothing(env: Environment) -> None:
    result = operations.execute(_write("p.txt", "should not land", apply=False), env)

    assert result.status is OperationStatus.PREVIEWED
    assert result.succeeded is False
    assert bool(result) is False
    assert result.effects.complete is False
    assert result.effects.bytes_written == 0
    assert not (env.work_root / "p.txt").exists()


def test_edit_previews_by_default_and_touches_nothing(env: Environment) -> None:
    (env.work_root / "e2.txt").write_text("original", encoding="utf-8")
    result = operations.execute(_edit("e2.txt", "original", "changed", apply=False), env)

    assert result.status is OperationStatus.PREVIEWED
    assert result.effects.complete is False
    assert (env.work_root / "e2.txt").read_text(encoding="utf-8") == "original"


def test_apply_true_actually_writes(env: Environment) -> None:
    result = operations.execute(_write("applied.txt", "go", apply=True), env)
    assert result.status is OperationStatus.SUCCEEDED
    assert (env.work_root / "applied.txt").read_text(encoding="utf-8") == "go"


# --- path confinement: '..' and symlink escapes are both refused --------


def test_write_refuses_a_dotdot_escape(env: Environment) -> None:
    result = operations.execute(_write("../outside.txt", "x"), env)
    assert result.status is OperationStatus.FAILED
    assert "escapes the confined root" in result.error
    assert not (env.work_root.parent / "outside.txt").exists()


def test_write_refuses_a_symlink_escape(env: Environment, tmp_path: Path) -> None:
    """A symlink inside the work root pointing outside it must not be a back door."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = env.work_root / "escape_link"
    link.symlink_to(outside, target_is_directory=True)

    result = operations.execute(_write("escape_link/evil.txt", "x"), env)

    assert result.status is OperationStatus.FAILED
    assert "escapes the confined root" in result.error
    assert not (outside / "evil.txt").exists()


def test_edit_refuses_a_symlink_escape(env: Environment, tmp_path: Path) -> None:
    outside = tmp_path / "outside2"
    outside.mkdir()
    (outside / "victim.txt").write_text("secret", encoding="utf-8")
    link = env.work_root / "escape_link2"
    link.symlink_to(outside, target_is_directory=True)

    result = operations.execute(_edit("escape_link2/victim.txt", "secret", "pwned"), env)

    assert result.status is OperationStatus.FAILED
    assert "escapes the confined root" in result.error
    assert (outside / "victim.txt").read_text(encoding="utf-8") == "secret"


def test_write_inside_the_root_via_a_symlinked_subdir_is_not_refused(
    env: Environment,
) -> None:
    """The confinement is about leaving the root, not about symlinks per se."""
    real_dir = env.work_root / "real"
    real_dir.mkdir()
    link = env.work_root / "linked"
    link.symlink_to(real_dir, target_is_directory=True)

    result = operations.execute(_write("linked/inside.txt", "fine"), env)

    assert result.status is OperationStatus.SUCCEEDED
    assert (real_dir / "inside.txt").read_text(encoding="utf-8") == "fine"


# --- check_write wiring: the gap this task also owns ---------------------


def test_write_into_a_declared_read_only_path_is_denied(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    work = tmp_path / "work"
    source.mkdir()
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    environment = Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        read_only_paths=(protected,),
    )

    result = operations.execute(_write("neighbours/peer/file.py", "x"), environment)

    assert result.status is OperationStatus.DENIED
    assert result.succeeded is False
    assert "read-only" in result.error
    assert not (protected / "peer" / "file.py").exists()


def test_edit_into_a_declared_read_only_path_is_denied(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    work = tmp_path / "work"
    source.mkdir()
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    (protected / "peer.py").write_text("original", encoding="utf-8")
    environment = Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        read_only_paths=(protected,),
    )

    result = operations.execute(_edit("neighbours/peer.py", "original", "changed"), environment)

    assert result.status is OperationStatus.DENIED
    assert (protected / "peer.py").read_text(encoding="utf-8") == "original"


def test_a_sibling_outside_the_read_only_subtree_still_succeeds(tmp_path: Path) -> None:
    """The whole point of check_write: it refuses the protected subtree only."""
    source = tmp_path / "checkout"
    work = tmp_path / "work"
    source.mkdir()
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    environment = Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        read_only_paths=(protected,),
    )

    result = operations.execute(_write("src/main.py", "print(1)"), environment)

    assert result.status is OperationStatus.SUCCEEDED
    assert (work / "src" / "main.py").read_text(encoding="utf-8") == "print(1)"


def test_check_write_refusal_is_distinct_from_the_run_command_approvals_gate(
    tmp_path: Path,
) -> None:
    """fs.* is never subject to the run_command policy — only to check_write.

    The final ``verdict`` on the result comes from the outer run_command gate
    (UNGATED for any fs.* kind, since that gate has no jurisdiction over
    structured filesystem operations at all) — the check_write reason still
    reaches the caller, but on ``error``/``rendering``, not on ``verdict``.
    """
    source = tmp_path / "checkout"
    work = tmp_path / "work"
    source.mkdir()
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    environment = Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        read_only_paths=(protected,),
    )

    result = operations.execute(_write("neighbours/x.py", "x"), environment)

    assert result.status is OperationStatus.DENIED
    assert result.verdict.decision is PolicyDecision.UNGATED
    assert "not subject to the run_command policy" in result.verdict.reason
    assert "read-only" in result.error


def test_check_write_is_ungated_when_no_read_only_paths_are_declared(
    env: Environment,
) -> None:
    """No read_only_paths declared -> nothing is refused by check_write."""
    result = operations.execute(_write("anywhere.py", "x"), env)
    assert result.status is OperationStatus.SUCCEEDED


# --- missing/malformed arguments are recoverable, never a run abort ------


def test_write_missing_path_is_a_recoverable_failed_result(env: Environment) -> None:
    operation = Operation(kind=fs_write.WRITE_KIND, arguments={"content": "x"}, apply=True)
    result = operations.execute(operation, env)
    assert result.status is OperationStatus.FAILED
    assert "path" in result.error


def test_edit_missing_old_string_is_a_recoverable_failed_result(env: Environment) -> None:
    (env.work_root / "m.txt").write_text("hi", encoding="utf-8")
    operation = Operation(
        kind=fs_write.EDIT_KIND,
        arguments={"path": "m.txt", "new_string": "y"},
        apply=True,
    )
    result = operations.execute(operation, env)
    assert result.status is OperationStatus.FAILED
    assert "old_string" in result.error


def test_edit_missing_file_is_a_recoverable_failed_result(env: Environment) -> None:
    result = operations.execute(_edit("does/not/exist.txt", "a", "b"), env)
    assert result.status is OperationStatus.FAILED
    assert "no such file" in result.error


def test_edit_no_op_is_refused(env: Environment) -> None:
    (env.work_root / "same.txt").write_text("same", encoding="utf-8")
    result = operations.execute(_edit("same.txt", "same", "same"), env)
    assert result.status is OperationStatus.FAILED
    assert "identical" in result.error


def test_a_malformed_call_never_raises_out_of_execute(env: Environment) -> None:
    """Confirms the pipeline contract: this is a FAILED result, not an exception."""
    operation = Operation(kind=fs_write.WRITE_KIND, arguments={}, apply=True)
    result = operations.execute(operation, env)  # must not raise
    assert result.status is OperationStatus.FAILED
