"""Unit tests for scripts/capture_colleague_baseline.py's own logic.

These do NOT need a live colleague checkout -- they exercise interpreter
selection and the SHA/environment-error paths with synthetic git repos and
empty directories, the same style ``tests/test_colleague_inventory.py`` uses
for its sibling scanner. They run everywhere, including CI.

Full end-to-end generation against real colleague is covered separately by
``test_regeneration_reproducible.py`` (skipped without a checkout).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.characterization.fixtures import FIXTURES_DIR

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "capture_colleague_baseline.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("capture_colleague_baseline", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


def _git_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)
    return tmp_path


# --- pinned identity ---------------------------------------------------


def test_pinned_sha_and_version_are_recorded():
    assert generator.PINNED_SHA == "28fee290c51fc4310b9fc576981809ad5c3132c6"
    assert generator.PINNED_VERSION == "1.51.0"


# --- interpreter selection ----------------------------------------------


def test_colleague_interpreter_prefers_the_checkouts_own_venv(tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)

    assert generator.colleague_interpreter(tmp_path) == [str(venv_python)]


def test_colleague_interpreter_falls_back_to_uv_run_without_a_venv(tmp_path):
    assert generator.colleague_interpreter(tmp_path) == [
        "uv",
        "run",
        "--project",
        str(tmp_path),
        "python3",
    ]


# --- SHA-mismatch guard (fails before ever spawning a capture child) -------


def test_generate_refuses_a_sha_mismatch_by_default(tmp_path):
    root = _git_repo(tmp_path / "checkout")

    with pytest.raises(generator.GeneratorError) as exc_info:
        generator.generate(root, tmp_path / "out")

    assert "pinned" in str(exc_info.value.hint) or "pinned" in str(exc_info.value)


def test_main_reports_a_sha_mismatch_as_exit_2(tmp_path, capsys):
    root = _git_repo(tmp_path / "checkout")

    rc = generator.main([str(root), "--out", str(tmp_path / "out")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


# --- missing colleague/ package (reached only via --allow-sha-mismatch) ---


def test_main_exits_2_on_a_checkout_with_no_colleague_package(tmp_path, capsys):
    rc = generator.main([str(tmp_path), "--allow-sha-mismatch"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_run_capture_environment_error_names_the_missing_package(tmp_path):
    with pytest.raises(generator.GeneratorError) as exc_info:
        generator.run_capture(tmp_path)

    assert "colleague" in str(exc_info.value)


# --- serialization shape used for every written fixture --------------------


def test_committed_fixtures_end_with_exactly_one_trailing_newline():
    """Every file generate() writes uses json.dumps(...) + "\\n" -- never
    zero trailing newlines, never two."""
    for filename in (
        generator.SCHEMAS_FILENAME,
        generator.BEHAVIOR_FILENAME,
        generator.META_FILENAME,
    ):
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        assert text.endswith("\n"), filename
        assert not text.endswith("\n\n"), filename
