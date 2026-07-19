"""Guard tests for the zero-base-dependency posture.

shell-cli's core may import **nothing outside the Python standard library,
ever**. This is not a preference: colleague pins exactly one base dependency
today and shell-cli is being allow-listed as the second sanctioned one. If
shell-cli takes a third-party dependency, colleague's own zero-deps guard fails
and colleague cannot import shell-cli at all.

This mirrors the colleague checkout's ``tests/test_zero_deps.py``. Its core
helper snapshots ``sys.modules`` before/after an action, reduces new entries to
top-level names, and filters stdlib (``sys.stdlib_module_names``), the own
package, and import-system builtins. Two patterns transfer directly and are used
here:

* run the check in a **fresh subprocess** when test-order independence matters
  (another test module may already have imported the leak, hiding it from an
  in-process before/after diff);
* **scan source text** when environment independence matters (a dependency that
  simply is not installed in this venv would otherwise pass by accident).

Unlike colleague, shell-cli allow-lists *nothing*: the sanctioned-third-party
slot is empty by design.
"""

from __future__ import annotations

import ast
import subprocess  # nosec B404 - fresh-interpreter import check, fixed argv
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_DIR = _REPO_ROOT / "shell"

# Known import-system builtins: not in sys.stdlib_module_names, but not
# third-party either. Carried over from colleague's guard.
_KNOWN_IMPORT_BUILTINS = {
    "importlib",
    "importlib_metadata",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "_bootstrap",
    "pip",
    "pkg_resources",
    "__main__",
    "__path__",
    "site",
    "sitecustomize",
    "usercustomize",
}

# The modules that make up the operation core. Kept explicit rather than
# globbed so that adding a module is a deliberate act that shows up in a diff.
_CORE_MODULES = (
    "shell",
    "shell.cli",
    "shell.environment",
    "shell.evidence",
    "shell.explain.catalog",
    "shell.fs",
    "shell.operations",
    "shell.process",
    "shell.results",
    "shell.runners",
    "shell.runners.host",
)


def _third_party_modules_introduced(action) -> list[str]:
    """Run ``action`` and return any third-party top-level modules it imports.

    Snapshots ``sys.modules`` before/after, reduces new entries to their
    top-level name, and filters stdlib, ``shell`` itself, and known
    import-system builtins. Anything left is a leak.
    """
    before = set(sys.modules.keys())
    action()
    new_top_level = {name.split(".")[0] for name in (set(sys.modules.keys()) - before) if name}

    third_party = []
    for name in sorted(new_top_level):
        is_stdlib = name in sys.stdlib_module_names
        is_own_package = name == "shell" or name.startswith("shell.")
        is_builtin = name in _KNOWN_IMPORT_BUILTINS or name.startswith("_")
        if not (is_stdlib or is_own_package or is_builtin):
            third_party.append(name)
    return third_party


def _python_sources() -> list[Path]:
    return sorted(_PACKAGE_DIR.rglob("*.py"))


def _imported_top_level_names(source: str) -> set[str]:
    """Top-level module names imported by *source*, ignoring relative imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; module is None for "from . import x"
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


# --- 1. the declaration -----------------------------------------------------


def test_base_dependencies_are_empty() -> None:
    """[project].dependencies is an empty list — no sanctioned base dep at all."""
    with open(_REPO_ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)

    dependencies = data.get("project", {}).get("dependencies")
    assert dependencies == [], (
        f"shell-cli must declare zero base dependencies, got {dependencies!r}. "
        "colleague allow-lists shell-cli in its own zero-deps guard; a base "
        "dependency here breaks that guard and the import with it."
    )


def test_pyproject_declares_dependencies_literally() -> None:
    """The literal ``dependencies = []`` line is present in pyproject.toml.

    Complements the parsed check: a *missing* ``dependencies`` key also parses
    as "no dependencies", but it is not the same commitment. The literal
    declaration is the thing a reader and a reviewer see.
    """
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in text, (
        "pyproject.toml must declare `dependencies = []` literally — an absent "
        "key parses the same but does not state the commitment."
    )


# --- 2. the runtime import graph -------------------------------------------


def test_no_third_party_imports() -> None:
    """Importing the operation core introduces no third-party top-level import."""

    def _import_core() -> None:
        import shell  # noqa: F401
        import shell.cli  # noqa: F401
        import shell.environment  # noqa: F401
        import shell.evidence  # noqa: F401
        import shell.explain.catalog  # noqa: F401
        import shell.fs  # noqa: F401
        import shell.operations  # noqa: F401
        import shell.process  # noqa: F401
        import shell.results  # noqa: F401
        import shell.runners  # noqa: F401
        import shell.runners.host  # noqa: F401

    third_party = _third_party_modules_introduced(_import_core)
    assert not third_party, (
        f"Third-party imports detected: {third_party}. "
        "The shell-cli core is pure-stdlib; expected only stdlib, shell, or builtins."
    )


def test_core_import_is_clean_in_a_fresh_subprocess() -> None:
    """The same check, in a fresh interpreter, so test order cannot hide a leak.

    An in-process ``sys.modules`` diff shows nothing new when an earlier test
    module in the same session already imported the offending package. A fresh
    subprocess starts from a clean ``sys.modules`` every time.
    """
    imports = "\n".join(f"import {name}" for name in _CORE_MODULES)
    code = (
        "import sys\n"
        f"{imports}\n"
        "builtins = " + repr(sorted(_KNOWN_IMPORT_BUILTINS)) + "\n"
        "leaks = sorted({\n"
        "    n.split('.')[0]\n"
        "    for n in sys.modules\n"
        "    if n\n"
        "    and n.split('.')[0] not in sys.stdlib_module_names\n"
        "    and n.split('.')[0] != 'shell'\n"
        "    and n.split('.')[0] not in builtins\n"
        "    and not n.startswith('_')\n"
        "})\n"
        "print('LEAKS:' + ','.join(leaks))\n"
    )
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell, trusted input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    leaks = completed.stdout.strip().removeprefix("LEAKS:")
    assert leaks == "", f"Fresh-interpreter import of the core pulled in: {leaks}"


# --- 3. the source text -----------------------------------------------------


def test_source_imports_only_stdlib_and_self() -> None:
    """Every import in shell/ resolves to stdlib or to shell itself.

    Environment-independent: a third-party import that simply is not installed
    in this venv would raise at import time in the runtime checks above, but a
    *conditional* or lazily-guarded one might not. Reading the source catches it
    regardless of what happens to be installed.
    """
    violations: list[str] = []
    for path in _python_sources():
        for name in sorted(_imported_top_level_names(path.read_text(encoding="utf-8"))):
            if name in sys.stdlib_module_names or name == "shell":
                continue
            if name in _KNOWN_IMPORT_BUILTINS or name.startswith("_"):
                continue
            violations.append(f"{path.relative_to(_REPO_ROOT)}: imports {name!r}")

    assert not violations, "Non-stdlib imports found in shell/:\n" + "\n".join(violations)


def test_the_source_scan_can_actually_fail(tmp_path: Path) -> None:
    """The scanner detects a third-party import — it is not vacuously green."""
    sample = "import os\nimport requests\nfrom . import sibling\nfrom shell.results import X\n"
    names = _imported_top_level_names(sample)
    assert "requests" in names
    assert "os" in names
    assert "shell" in names
    assert "sibling" not in names, "relative imports must not be treated as top-level"
