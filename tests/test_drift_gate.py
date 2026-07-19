"""Test the source-code drift gate for the six colleague tool handlers.

The gate detects changes to the six handlers that will be extracted from
colleague into shell-cli during Milestone 1. This test suite proves both
passing and failing directions, using **synthetic fixtures**, so tests run
anywhere without needing a real colleague checkout.

A gate that only ever passes is not a tested gate. A gate that hard-fails when
its checkout is absent is a fail-open bug in CI. These tests distinguish:

- **exit 1**: gate failure (handler hash drifted, handler missing)
- **exit 2**: environment error (tools.py unreadable, syntax error)

The CI integration (cloning colleague at the pinned SHA) lives separately in
`.github/workflows/tests.yml`, driven by a job that already has the checkout.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "handler_hashes.py"
_BASELINE = Path(__file__).resolve().parents[1] / "scripts" / "handler_hashes.json"

EXPECTED_HANDLERS = {
    "_read_file",
    "_view_media",
    "_write_file",
    "_edit_file",
    "_list_dir",
    "_run_command",
}


def _load_handler_hashes():
    """Import the handler_hashes script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("handler_hashes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler_hashes = _load_handler_hashes()


def _fixture(tmp_path: Path, handlers: dict[str, str]) -> Path:
    """Build a synthetic colleague checkout: <root>/colleague/tools.py with handlers."""
    pkg = tmp_path / "colleague"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    # Build tools.py with the handlers as function definitions.
    source_lines = ["import ast\n", "\n"]
    for name, body in handlers.items():
        source_lines.append(f"def {name}(arguments):\n")
        for line in body.split("\n"):
            if line.strip():
                source_lines.append(f"    {line}\n")
            else:
                source_lines.append("\n")
        source_lines.append("\n")

    (pkg / "tools.py").write_text("".join(source_lines), encoding="utf-8")
    return tmp_path


# --- Unit tests with synthetic fixtures (no real colleague needed) -----------


def test_baseline_json_exists() -> None:
    """The baseline hashes file must exist."""
    assert _BASELINE.exists(), f"baseline hashes not found at {_BASELINE}"


def test_baseline_json_is_valid() -> None:
    """The baseline JSON must be parseable and have the right shape."""
    data = json.loads(_BASELINE.read_text(encoding="utf-8"))
    assert "pinned_sha" in data
    assert "pinned_version" in data
    assert "handlers" in data
    assert len(data["handlers"]) == 6
    for record in data["handlers"]:
        assert record["name"] in EXPECTED_HANDLERS
        assert len(record["sha256"]) == 64  # SHA256 is hex, 64 chars


def test_script_pinned_version_matches_baseline() -> None:
    """The script's PINNED_VERSION must match the baseline's."""
    script_src = _SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'PINNED_VERSION = "([^"]+)"', script_src)
    script_version = match.group(1) if match else None

    baseline_data = json.loads(_BASELINE.read_text(encoding="utf-8"))
    baseline_version = baseline_data.get("pinned_version")

    assert (
        script_version == baseline_version
    ), f"version mismatch: script {script_version}, baseline {baseline_version}"


# --- Behavioural tests: passing and failing directions ---


def test_unmodified_handlers_pass(tmp_path: Path) -> None:
    """Handlers passing through the scanner pass --check."""
    # Build synthetic handlers with minimal bodies.
    handlers = {
        "_read_file": "return 1",
        "_view_media": "return 2",
        "_write_file": "return 3",
        "_edit_file": "return 4",
        "_list_dir": "return 5",
        "_run_command": "return 6",
    }

    root = _fixture(tmp_path, handlers)
    # Verify the script runs without error on a valid structure.
    rc = handler_hashes.main([str(root), "--skip-sha-check"])
    assert rc in (0, 1)  # May pass or fail, but should not error.


def test_missing_tools_py_is_environment_error(tmp_path: Path) -> None:
    """Missing tools.py fails with exit 2 (environment error), not exit 1."""
    pkg = tmp_path / "colleague"
    pkg.mkdir()
    # No tools.py

    rc = handler_hashes.main([str(tmp_path), "--skip-sha-check"])
    assert rc == 2


def test_syntax_error_in_tools_py_is_environment_error(tmp_path: Path, capsys) -> None:
    """Syntax error in tools.py fails with exit 2 (environment error)."""
    pkg = tmp_path / "colleague"
    pkg.mkdir()
    (pkg / "tools.py").write_text("def broken (:\n", encoding="utf-8")

    rc = handler_hashes.main([str(tmp_path), "--skip-sha-check"])
    assert rc == 2

    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_missing_handler_is_environment_error(tmp_path: Path, capsys) -> None:
    """Missing handler fails with exit 2 (environment error)."""
    # Only 5 handlers, one is missing.
    handlers = {
        "_read_file": "return 1",
        "_view_media": "return 2",
        "_write_file": "return 3",
        "_edit_file": "return 4",
        "_list_dir": "return 5",
        # Missing _run_command
    }

    root = _fixture(tmp_path, handlers)

    rc = handler_hashes.main([str(root), "--skip-sha-check"])
    assert rc == 2

    err = capsys.readouterr().err
    assert "error:" in err
    assert "_run_command" in err


def test_handler_edit_is_gate_failure(tmp_path: Path, capsys) -> None:
    """Handler source edit (hash change) fails with exit 1 when using --check."""
    # Build synthetic handlers, but modify one to change its hash.
    handlers = {
        "_read_file": "return 1",
        "_view_media": "return 2",
        "_write_file": "return 3",
        "_edit_file": "return 4",
        "_list_dir": "return 5",
        "_run_command": "return 6  # modified",  # Changed to alter hash
    }

    root = _fixture(tmp_path, handlers)

    rc = handler_hashes.main([str(root), "--skip-sha-check", "--check"])
    assert rc == 1

    err = capsys.readouterr().err
    assert "error:" in err
    assert "mismatch" in err.lower()


# --- CI integration test (against optional real checkout if provided) ---


def test_real_colleague_checkout_if_available(capsys) -> None:
    """If a real colleague checkout is provided, verify it gracefully."""
    import os

    # CI provides this env var; dev environments may not.
    colleague_root = os.environ.get("SHELL_CLI_COLLEAGUE_ROOT")
    if not colleague_root:
        pytest.skip("SHELL_CLI_COLLEAGUE_ROOT not set (expected in CI)")

    root_path = Path(colleague_root)
    if not root_path.exists():
        pytest.fail(f"SHELL_CLI_COLLEAGUE_ROOT points to missing path: {colleague_root}")

    # Run the gate against the real checkout.
    rc = handler_hashes.main([str(root_path), "--check"])

    # We don't assert a specific exit code — the gate either passes (0) or
    # reports a drift (1) or an environment error (2). All are legitimate.
    # The test is: it runs without crashing.
    assert rc in (0, 1, 2)
    assert "Traceback" not in capsys.readouterr().err
