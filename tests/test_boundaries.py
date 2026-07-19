"""Guard tests for the two repository boundaries shell-cli must never cross.

**colleague** is the first consumer, not the owner: colleague imports shell-cli,
and shell-cli never imports colleague. An import in this direction would make the
extraction circular and would drag colleague's domain contracts (``ToolOutcome``,
``Step``, roles, hooks) into a package whose whole point is that it holds none of
them.

**webglass-cli** is a *peer, not a layer*. Browser sessions, page fetches,
navigation and search results are web semantics and belong entirely to
webglass-cli; colleague composes both packages and neither composes the other.
Only provider-neutral artifacts cross the seam — when webglass produces a file it
is just a file, read through the ordinary confined filesystem path with no
webglass type in any signature.

Both directions are checked twice: against the live import graph, and against the
source text (so a lazily-guarded or conditional import cannot slip past a runtime
snapshot).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_DIR = _REPO_ROOT / "shell"

# Top-level package names that shell-cli must never import. Matched on the
# top-level name and on any dotted prefix of it.
_FORBIDDEN = ("colleague", "webglass", "webglass_cli")


def _python_sources() -> list[Path]:
    return sorted(_PACKAGE_DIR.rglob("*.py"))


def _imported_module_names(source: str) -> set[str]:
    """Fully-qualified module names imported by *source* (absolute imports only)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _is_forbidden(module_name: str) -> bool:
    top = module_name.split(".")[0]
    return top in _FORBIDDEN


# --- the live import graph --------------------------------------------------


def test_importing_the_core_pulls_in_no_forbidden_package() -> None:
    """Importing every shell module leaves colleague/webglass out of sys.modules."""
    before = set(sys.modules)

    import shell  # noqa: F401
    import shell.cli  # noqa: F401
    import shell.environment  # noqa: F401
    import shell.fs  # noqa: F401
    import shell.operations  # noqa: F401
    import shell.process  # noqa: F401
    import shell.results  # noqa: F401
    import shell.runners  # noqa: F401
    import shell.runners.host  # noqa: F401

    introduced = {name.split(".")[0] for name in (set(sys.modules) - before)}
    offenders = sorted(name for name in introduced if name in _FORBIDDEN)
    assert not offenders, (
        f"Importing shell-cli pulled in {offenders}. colleague imports shell-cli, "
        "never the reverse; webglass-cli is a peer, not a layer."
    )


# --- the source text --------------------------------------------------------


@pytest.mark.parametrize("forbidden", _FORBIDDEN)
def test_no_source_file_imports_the_forbidden_package(forbidden: str) -> None:
    violations: list[str] = []
    for path in _python_sources():
        for module in sorted(_imported_module_names(path.read_text(encoding="utf-8"))):
            if module.split(".")[0] == forbidden:
                violations.append(f"{path.relative_to(_REPO_ROOT)}: imports {module!r}")

    assert not violations, f"shell-cli must never import {forbidden!r}:\n" + "\n".join(violations)


def test_the_boundary_scan_can_actually_fail() -> None:
    """The scanner detects a forbidden import — it is not vacuously green."""
    names = _imported_module_names(
        "import os\nfrom colleague.tools import ToolExecutor\nimport webglass.session\n"
    )
    assert {n for n in names if _is_forbidden(n)} == {"colleague.tools", "webglass.session"}
    assert not _is_forbidden("shell.operations")
    assert not _is_forbidden("os")


# --- the semantic boundary --------------------------------------------------


def test_no_registered_operation_kind_claims_web_semantics() -> None:
    """A ``web.*`` operation kind would be the webglass seam collapsing.

    Web semantics belong to webglass-cli entirely. shell-cli reads whatever file
    a browser produced through the ordinary confined filesystem path and never
    learns that a browser produced it.
    """
    from shell import operations

    offenders = [kind for kind in operations.registered_kinds() if kind.split(".")[0] == "web"]
    assert not offenders, (
        f"operation kinds claiming web semantics: {offenders}. Web semantics belong "
        "to webglass-cli; only provider-neutral artifacts cross the seam."
    )
