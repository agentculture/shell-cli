"""Tests for ``shell operation`` — ``show``, ``overview``.

The central property this file proves: **the same ``Operation``, run once
through the library API, is retrievable through the CLI with an identical
status, policy verdict, and effects.** That is done with a REAL ``fs.read``
operation (``shell/fs/read.py``, the real registered ``fs.read`` kind), never
a synthetic operation kind invented only to make the test pass —
``shell.operations.execute()`` is called directly with an
:class:`~shell.evidence.EvidenceStore` configured, and the CLI's
``operation show`` is then pointed at the exact directory that run wrote to.
The CLI never gets a shortcut into the evidence store's internals; it goes
through the same :class:`~shell.evidence.EvidenceStore` class the library
used to write the record.

This module imports ``shell.fs.read`` explicitly at module scope so the
handler is registered in every pytest worker under ``pytest -n auto`` — not
relying on some other test module having imported it first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import shell.fs.read  # noqa: F401  (registers fs.read as a real operation kind)
from shell import operations
from shell.cli import main
from shell.environment import Environment
from shell.evidence import EvidenceStore
from shell.fs.read import KIND as FS_READ_KIND
from shell.operations import Operation
from shell.runners.host import HostRunner

# --- overview ---------------------------------------------------------------


def test_operation_bare_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["operation"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# shell operation" in out
    assert "show" in out


def test_operation_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["operation", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "shell operation"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


# --- library/CLI equivalence, with a REAL fs.read operation -----------------


@pytest.fixture
def source_and_work(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return source, work


def test_operation_show_matches_the_library_result_for_a_real_fs_read(
    source_and_work: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    source, work = source_and_work
    (work / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")

    environment = Environment(source_root=source, work_root=work, runner=HostRunner())
    store = EvidenceStore.for_environment(environment)
    operation = Operation(kind=FS_READ_KIND, arguments={"path": "hello.txt"})

    # THE library execution path.
    result = operations.execute(operation, environment, evidence_store=store)
    assert result.succeeded

    evidence_dir = store.directory
    rc = main(["operation", "show", operation.id, "--evidence-dir", str(evidence_dir), "--json"])
    assert rc == 0
    record = json.loads(capsys.readouterr().out)

    # Same operation id, same status, same policy verdict, same effects --
    # retrieved through the CLI, produced by the library.
    assert record["operation_id"] == operation.id == result.operation_id
    assert record["status"] == result.status.value == "succeeded"
    assert record["policy"]["decision"] == result.verdict.decision.value == "ungated"
    assert record["policy"]["reason"] == result.verdict.reason
    assert record["effects"]["complete"] == result.effects.complete is True
    assert record["effects"]["changed_paths"] == list(result.effects.changed_paths) == []
    assert record["effects"]["bytes_written"] == result.effects.bytes_written == 0
    assert record["operation"]["kind"] == FS_READ_KIND == "fs.read"


def test_operation_show_text_mode_reports_the_same_status(
    source_and_work: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    source, work = source_and_work
    (work / "hello.txt").write_text("hi\n", encoding="utf-8")

    environment = Environment(source_root=source, work_root=work, runner=HostRunner())
    store = EvidenceStore.for_environment(environment)
    operation = Operation(kind=FS_READ_KIND, arguments={"path": "hello.txt"})
    result = operations.execute(operation, environment, evidence_store=store)

    rc = main(["operation", "show", operation.id, "--evidence-dir", str(store.directory)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"operation {operation.id}" in out
    assert f"status: {result.status.value}" in out
    assert "policy: ungated" in out


def test_operation_show_a_failed_operation_reports_failed_status(
    source_and_work: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Not just the happy path -- a FAILED fs.read (missing file) round-trips too."""
    source, work = source_and_work
    environment = Environment(source_root=source, work_root=work, runner=HostRunner())
    store = EvidenceStore.for_environment(environment)
    operation = Operation(kind=FS_READ_KIND, arguments={"path": "does-not-exist.txt"})

    result = operations.execute(operation, environment, evidence_store=store)
    assert not result.succeeded
    assert result.status.value == "failed"

    rc = main(["operation", "show", operation.id, "--evidence-dir", str(store.directory), "--json"])
    assert rc == 0
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "failed" == result.status.value
    assert record["execution"]["error"] == result.error


# --- honest "no trail" reporting ---------------------------------------------


def test_operation_show_no_evidence_directory_is_a_structured_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Raised inside the handler (not by argparse), so main() returns the exit
    # code directly rather than raising SystemExit -- see
    # test_explain_unknown_path_errors in tests/test_cli.py for the same shape.
    missing_dir = tmp_path / "nowhere" / "evidence"
    rc = main(["operation", "show", "some-id", "--evidence-dir", str(missing_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "no evidence directory" in err
    assert "opt-in" in err
    assert "hint:" in err


def test_operation_show_unmatched_id_reports_how_many_records_exist(
    source_and_work: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    source, work = source_and_work
    (work / "hello.txt").write_text("hi\n", encoding="utf-8")
    environment = Environment(source_root=source, work_root=work, runner=HostRunner())
    store = EvidenceStore.for_environment(environment)
    operation = Operation(kind=FS_READ_KIND, arguments={"path": "hello.txt"})
    operations.execute(operation, environment, evidence_store=store)

    rc = main(["operation", "show", "not-a-real-id", "--evidence-dir", str(store.directory)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no evidence record for operation" in err
    assert "1 record(s) present" in err


def test_operation_show_evidence_path_not_a_directory_is_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_dir = tmp_path / "evidence-is-a-file"
    not_a_dir.write_text("oops", encoding="utf-8")

    rc = main(["operation", "show", "some-id", "--evidence-dir", str(not_a_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "not a directory" in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses permission bits; the environment-error path is untestable as root",
)
def test_operation_show_unreadable_directory_is_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = tmp_path / "blocked-evidence"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        rc = main(["operation", "show", "some-id", "--evidence-dir", str(blocked)])
        assert rc == 2
        err = capsys.readouterr().err
        assert err.startswith("error:")
        assert "could not be read" in err
    finally:
        blocked.chmod(0o755)


# --- structured error contract ----------------------------------------------


def test_operation_bogus_subcommand_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["operation", "bogus-verb"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_operation_show_missing_operation_id_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["operation", "show"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- catalog integration -----------------------------------------------------


def test_operation_paths_resolve_via_explain(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (["operation"], ["operation", "show"]):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        assert "# shell operation" in capsys.readouterr().out
