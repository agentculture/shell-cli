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
from pathlib import Path, PurePosixPath, PureWindowsPath

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
_BROKEN = "def (:\n"


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


def test_unparseable_module_is_recorded_as_skipped_not_fatal(tmp_path):
    """A syntax error must not crash the gate — and must not be silent either.

    Returning [] for an unparseable file is fail-OPEN: part of the checkout was
    never scanned, yet `--check` would pass and `debt_remaining` would look
    clean. The gate must say which files it could not read.
    """
    root = _fixture(tmp_path, {"broken.py": _BROKEN, "tools.py": _SPAWNS})

    inv = inventory.scan(root)

    assert inv.modules == {"tools.py"}
    assert "broken.py" in inv.skipped
    assert "SyntaxError" in inv.skipped["broken.py"]


def test_check_fails_with_exit_2_when_a_file_was_skipped(tmp_path, capsys):
    """A partial scan cannot be trusted, so `--check` fails with exit 2.

    Exit 2 (the scanner itself is broken) is deliberately distinct from exit 1
    (the scan ran and found an unclassified path), matching the convention
    already used for a missing checkout.
    """
    root = _fixture(tmp_path, {"broken.py": _BROKEN, "tools.py": _SPAWNS})

    assert inventory.main([str(root), "--check"]) == 2

    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_skipped_files_win_over_unclassified_paths(tmp_path):
    """An untrustworthy scan reports exit 2, not the exit-1 gate verdict."""
    root = _fixture(tmp_path, {"broken.py": _BROKEN, "rogue.py": _SPAWNS})

    assert inventory.main([str(root), "--check"]) == 2


def test_skipped_files_are_published_in_json_and_text(tmp_path, capsys):
    """CI must be able to see a degraded scan in both output modes."""
    root = _fixture(tmp_path, {"broken.py": _BROKEN})

    assert inventory.main([str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_count"] == 1
    assert any("broken.py" in entry for entry in payload["skipped"])

    assert inventory.main([str(root)]) == 0
    assert "broken.py" in capsys.readouterr().out


def test_clean_scan_reports_no_skips(tmp_path, capsys):
    """The skipped surface stays empty when every file parses."""
    root = _fixture(tmp_path, {"tools.py": _SPAWNS})

    assert inventory.main([str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == []
    assert payload["skipped_count"] == 0


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


# --- import aliasing: the evasion the gate must not have -------------------


@pytest.mark.parametrize(
    ("source", "call"),
    [
        ("import subprocess as sp\nsp.run(['true'])\n", "subprocess.run"),
        ("import subprocess as sp\nsp.Popen(['true'])\n", "subprocess.Popen"),
        ("from subprocess import run\nrun(['true'])\n", "subprocess.run"),
        ("from subprocess import run as r\nr(['true'])\n", "subprocess.run"),
        ("from subprocess import Popen as P\nP(['true'])\n", "subprocess.Popen"),
        ("from os import system\nsystem('true')\n", "os.system"),
        ("import os as _o\n_o.system('true')\n", "os.system"),
        (
            "import asyncio as aio\naio.create_subprocess_shell('true')\n",
            "asyncio.create_subprocess_shell",
        ),
        (
            "from asyncio import create_subprocess_exec as cse\ncse('true')\n",
            "asyncio.create_subprocess_exec",
        ),
    ],
)
def test_aliased_imports_do_not_evade_the_scanner(tmp_path, source, call):
    """`import subprocess as sp` must not launder a spawn past the gate.

    Matching only the literal dotted text is a false negative by construction,
    and a false negative defeats the entire purpose of an enforcement gate.
    """
    root = _fixture(tmp_path, {"rogue.py": source})

    inv = inventory.scan(root)

    assert [f.call for f in inv.findings] == [call]
    assert [f.module for f in inv.unclassified] == ["rogue.py"]


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess as sp\nsp.run(['true'])\n",
        "from subprocess import run\nrun(['true'])\n",
    ],
)
def test_check_fails_on_an_aliased_unclassified_spawn(tmp_path, source):
    """End to end: the gate exits 1, it does not merely record the finding."""
    root = _fixture(tmp_path, {"rogue.py": source})

    assert inventory.main([str(root), "--check"]) == 1


def test_aliased_shell_true_is_still_surfaced(tmp_path, capsys):
    """shell=True detection must survive the alias resolution."""
    root = _fixture(tmp_path, {"hooks.py": "import subprocess as sp\nsp.run('x', shell=True)\n"})

    assert inventory.main([str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["shell_true_sites"] == ["hooks.py:2"]


@pytest.mark.parametrize(
    "source",
    [
        # A local def named `run` — never imported from subprocess.
        "def run(x):\n    pass\n\n\nrun(['true'])\n",
        # A same-named function from an unrelated module.
        "from mytasks import run\nrun(['true'])\n",
        # A relative import that merely happens to be spelled `subprocess`.
        "from .subprocess import run\nrun(['true'])\n",
        # A method call on an object, not the module.
        "class C:\n    def run(self):\n        pass\n\n\nC().run()\n",
        # An attribute on a local object that shadows nothing imported.
        "import mything as os\nos.system('true')\n",
    ],
)
def test_unimported_names_are_not_false_positives(tmp_path, source):
    """Binding resolution must come from real import statements only."""
    root = _fixture(tmp_path, {"rogue.py": source})

    assert inventory.scan(root).findings == []


# --- module keys are POSIX-normalized, on every host -----------------------


def test_module_key_is_posix_on_a_windows_style_path():
    """`str(path.relative_to(pkg))` yields `resident\\steward.py` on Windows,
    which can never match the forward-slash ALLOWLIST key — a false
    'unclassified' and a spurious gate failure. Assert against a pure Windows
    path so the guarantee does not depend on the host separator."""
    pkg = PureWindowsPath(r"C:\src\colleague\colleague")
    path = pkg / "resident" / "steward.py"

    key = inventory.module_key(path, pkg)

    assert key == "resident/steward.py"
    assert key in inventory.ALLOWLIST


def test_module_key_is_stable_on_posix_paths():
    pkg = PurePosixPath("/src/colleague/colleague")

    assert inventory.module_key(pkg / "tools.py", pkg) == "tools.py"
    assert inventory.module_key(pkg / "resident" / "steward.py", pkg) == "resident/steward.py"


def test_allowlist_keys_are_posix_form():
    """The ALLOWLIST is the contract module_key normalizes toward."""
    assert all("\\" not in key for key in inventory.ALLOWLIST)
