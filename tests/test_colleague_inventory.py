"""The known-debt gate: `colleague_inventory.py --check`.

The gate exists so a *new* unclassified process-spawn path in colleague fails
CI immediately, while the already-known paths are tracked as scheduled
migrations that must reach zero by Milestone 3.

A gate that only ever runs against a checkout where it passes is not a tested
gate. These tests drive both directions against **synthetic fixtures**, so they
prove the failing direction without needing a real colleague checkout — which
CI does not have when this file runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "colleague_inventory.py"


def _load_scanner():
    """Import the scanner by path — `scripts/` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("colleague_inventory", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inventory = _load_scanner()


def _fixture(tmp_path: Path, modules: dict[str, str]) -> Path:
    """Build a synthetic colleague checkout: <root>/colleague/<module>.py."""
    pkg = tmp_path / "colleague"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for rel, source in modules.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tmp_path


_SPAWNS = "import subprocess\n\n\ndef go():\n    subprocess.run(['true'], check=False)\n"
_SPAWNS_SHELL = "import subprocess\n\n\ndef go():\n    subprocess.run('true', shell=True)\n"
_INERT = "def go():\n    return 'subprocess.run is only a string here'\n"


# --- the failing direction: a NEW unclassified spawn path ------------------


def test_unclassified_spawn_is_reported(tmp_path):
    """A spawning module absent from ALLOWLIST is unclassified."""
    root = _fixture(tmp_path, {"rogue.py": _SPAWNS})
    assert "rogue.py" not in inventory.ALLOWLIST

    inv = inventory.scan(root)

    assert [f.module for f in inv.unclassified] == ["rogue.py"]
    assert inv.findings[0].call == "subprocess.run"


def test_check_exits_nonzero_on_unclassified_spawn(tmp_path, capsys):
    """--check must FAIL when an unclassified path appears. This is the gate."""
    root = _fixture(tmp_path, {"rogue.py": _SPAWNS})

    assert inventory.main([str(root), "--check"]) != 0

    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err


def test_check_flags_unclassified_even_beside_allowlisted(tmp_path):
    """One rogue module fails the gate even when known-debt modules dominate."""
    root = _fixture(tmp_path, {"tools.py": _SPAWNS, "rogue.py": _SPAWNS})

    assert inventory.main([str(root), "--check"]) == 1


# --- the passing direction: only allow-listed spawn paths ------------------


def test_check_exits_zero_when_all_spawns_are_allowlisted(tmp_path):
    """Every spawning module classified in ALLOWLIST -> gate passes."""
    root = _fixture(tmp_path, {"tools.py": _SPAWNS, "worktrees.py": _SPAWNS})

    assert inventory.main([str(root), "--check"]) == 0


def test_check_exits_zero_with_no_spawns_at_all(tmp_path):
    """A spawn-free checkout is vacuously clean, not an error."""
    root = _fixture(tmp_path, {"quiet.py": _INERT})

    inv = inventory.scan(root)
    assert inv.findings == []
    assert inventory.main([str(root), "--check"]) == 0


# --- what CI publishes -----------------------------------------------------


def test_json_payload_publishes_debt_remaining(tmp_path, capsys):
    """CI reads debt_remaining off --json; it is derived from ALLOWLIST."""
    root = _fixture(tmp_path, {"tools.py": _SPAWNS, "background.py": _SPAWNS})

    assert inventory.main([str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["spawn_sites"] == 2
    assert payload["modules"] == 2
    assert payload["unclassified"] == []
    # background.py is debt=False (stays in colleague); tools.py is debt=True.
    assert payload["debt_modules"] == ["tools.py"]
    assert payload["debt_remaining"] == 1
    assert payload["by_profile"] == {"project": 1, "control": 1}


def test_debt_remaining_counts_only_debt_true_modules():
    """The committed ALLOWLIST is the source of the published counter."""
    debt = [m for m, (_, is_debt) in inventory.ALLOWLIST.items() if is_debt]
    assert len(debt) == 13, "debt baseline changed — update CLAUDE.md's M3 target too"


def test_pinned_sha_is_recorded(tmp_path, capsys):
    """The gate names the commit it was characterized against."""
    assert inventory.PINNED_SHA == "28fee290c51fc4310b9fc576981809ad5c3132c6"
    assert inventory.PINNED_VERSION == "1.51.0"

    root = _fixture(tmp_path, {"quiet.py": _INERT})
    assert inventory.main([str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pinned_sha"] == inventory.PINNED_SHA


def test_shell_true_sites_are_surfaced(tmp_path, capsys):
    """shell=True is the highest-risk shape; it is reported explicitly."""
    root = _fixture(tmp_path, {"hooks.py": _SPAWNS_SHELL})

    assert inventory.main([str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shell_true_sites"] == ["hooks.py:5"]


# --- scanner robustness ----------------------------------------------------


def test_missing_colleague_package_is_an_environment_error(tmp_path, capsys):
    """A broken invocation must fail loudly with exit 2 and no traceback.

    Exit 2 (environment error) is distinct from exit 1 (gate failure) so CI can
    tell "the clone/setup is broken" from "a new unclassified path landed". A
    gate that silently no-ops is worse than no gate.
    """
    rc = inventory.main([str(tmp_path), "--check"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_unparseable_module_is_skipped_not_fatal(tmp_path):
    """A syntax error in colleague must not crash the gate."""
    root = _fixture(tmp_path, {"broken.py": "def (:\n", "tools.py": _SPAWNS})

    inv = inventory.scan(root)
    assert inv.modules == {"tools.py"}


def test_local_name_shadowing_a_spawn_call_is_not_counted(tmp_path):
    """AST matching is on the dotted path — a local `run()` is not a spawn."""
    root = _fixture(tmp_path, {"rogue.py": "def run(x):\n    pass\n\n\nrun(['true'])\n"})

    assert inventory.scan(root).findings == []


def test_nested_module_paths_are_reported_relative_to_package(tmp_path):
    """resident/steward.py is allow-listed by its package-relative path."""
    root = _fixture(tmp_path, {"resident/steward.py": _SPAWNS})

    inv = inventory.scan(root)
    assert inv.modules == {"resident/steward.py"}
    assert inv.unclassified == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.Popen(['true'])\n",
        "import subprocess\nsubprocess.check_output(['true'])\n",
        "import os\nos.system('true')\n",
        "import asyncio\nasyncio.create_subprocess_exec('true')\n",
    ],
)
def test_every_spawn_shape_is_detected(tmp_path, source):
    """os.system and the asyncio spawners are zero in colleague today — but the
    gate must still catch them if one appears."""
    root = _fixture(tmp_path, {"rogue.py": source})

    assert inventory.main([str(root), "--check"]) == 1
