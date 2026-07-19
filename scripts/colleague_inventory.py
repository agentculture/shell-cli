#!/usr/bin/env python3
"""Reproducible process-spawn inventory for a colleague checkout.

Milestone 0 of the shell-cli build brief
(https://github.com/agentculture/shell-cli/issues/1) requires an inventory of
every process/workspace mutation path in colleague, classified as ``project``,
``control``, ``observe``, or runtime-private bookkeeping.

A hand-written inventory rots the moment colleague moves. This script makes the
inventory *reproducible* and pins it to an exact commit, so drift is detected
mechanically instead of re-surveyed by hand.

Pure stdlib, read-only, AST-based (a regex over source text miscounts spawns
inside strings and comments).

Usage::

    python3 scripts/colleague_inventory.py /home/spark/git/colleague
    python3 scripts/colleague_inventory.py /path/to/colleague --json
    python3 scripts/colleague_inventory.py /path/to/colleague --check

``--check`` exits 1 when an unclassified spawn path exists. Exit 2 is reserved
for a scanner/environment error (a missing or unreadable checkout, or a file
that could not be parsed) so CI can distinguish "a new unclassified path
landed" from "the scan itself cannot be trusted" — a check that silently
no-ops is worse than no check.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------

This is a **drift detector against a pinned baseline**. It is *not* an
enforcement boundary, and nothing here should be described as one.

An adversarial live test (issue #7) landed **30 executed evasions at exit 0**.
Three limits are worth knowing before you rely on any number this prints:

* ``ALLOWLIST`` is keyed per **module**, not per **site**. A brand-new spawn
  added to an already-allow-listed module — including ``shell=True`` — is
  invisible by design. 15 of colleague's modules are already allow-listed.
* ``_SPAWN_CALLS`` is a literal set. ``subprocess.getoutput`` and ~14 other
  real spawn APIs are not in it and are not detected.
* Resolution follows *import* bindings only. Assignment aliasing
  (``sp = subprocess``), ``getattr``, dynamic import, and sibling re-export all
  defeat it.

What it does well is reproduce a known inventory at an exact commit and notice
when that inventory moves. That is genuinely useful and it is the whole claim.
A static AST scan cannot stop a determined author, so the honest ceiling here
is the same posture the rest of this repo commits to: it catches accidental and
careless drift, not adversarial evasion.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess  # nosec B404 - reads git metadata from a local checkout
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePath

# The commit this inventory was recorded against. Reproducing the numbers on a
# different commit is expected to differ — that is the point of pinning.
PINNED_SHA = "28fee290c51fc4310b9fc576981809ad5c3132c6"
PINNED_VERSION = "1.51.0"

# Callables that create a process. Matched on the *resolved* dotted path: call
# targets are first mapped back through the module's own import statements, so
# ``sp.run`` (after ``import subprocess as sp``) and a bare ``run`` (after
# ``from subprocess import run``) both resolve to ``subprocess.run``, while a
# locally defined ``run`` — bound by no import — does not.
_SPAWN_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execvp",
    "os.spawnv",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}

# Modules permitted to spawn a process, with the profile each one operates
# under. This is the KNOWN-DEBT ALLOWLIST: it records what exists today so a
# NEW unclassified path fails immediately, while the already-known paths are
# tracked as debt that must reach zero by Milestone 3.
#
# `debt=True`  -> a path shell-cli must take over; must be empty by M3.
# `debt=False` -> stays in colleague permanently (domain orchestration).
ALLOWLIST: dict[str, tuple[str, bool]] = {
    # profile   debt
    "tools.py": ("project", True),
    "affectedtests.py": ("project", True),
    "lint.py": ("project", True),
    "hooks.py": ("project", True),
    "livecheck.py": ("project", True),
    "worktrees.py": ("control", True),
    "handoff.py": ("control", True),
    "neighbours.py": ("control", True),
    "culture.py": ("control", True),
    "devague.py": ("control", True),
    "memory.py": ("control", True),
    "coherence.py": ("control", True),
    "resident/steward.py": ("control", True),
    "background.py": ("control", False),
    "experiment.py": ("control", False),
}


class InventoryError(Exception):
    """An environment problem: the checkout is missing or unreadable.

    Raised, never printed as a traceback. ``main`` renders it as
    ``error:``/``hint:`` on stderr and returns exit code 2.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class Finding:
    module: str
    line: int
    call: str
    uses_shell: bool = False


@dataclass
class Inventory:
    findings: list[Finding] = field(default_factory=list)
    modules: set[str] = field(default_factory=set)
    unclassified: list[Finding] = field(default_factory=list)
    # module -> why it could not be scanned. A non-empty map means the scan was
    # PARTIAL, so no verdict derived from it can be trusted.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def debt_modules(self) -> set[str]:
        return {m for m in self.modules if ALLOWLIST.get(m, ("", False))[1]}


class ScanSkipped(Exception):
    """A single file could not be parsed. Recorded, never swallowed."""


def module_key(path: PurePath, pkg: PurePath) -> str:
    """Package-relative module key, always forward-slash separated.

    ``str(path.relative_to(pkg))`` renders with the *host* separator, so on
    Windows a nested module becomes ``resident\\steward.py`` and can never match
    the forward-slash ALLOWLIST key ``resident/steward.py`` — turning a
    classified module into a false "unclassified" and failing CI for the wrong
    reason. ALLOWLIST keys are POSIX form; normalize toward them.
    """
    return path.relative_to(pkg).as_posix()


def _dotted(node: ast.AST) -> str:
    """Render an attribute/name chain as a dotted string, else ''."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _import_bindings(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Map local names back to the module paths their imports bound them to.

    Returns ``(module_aliases, direct_names)``:

    * ``module_aliases`` — ``import subprocess as sp`` -> ``{"sp": "subprocess"}``
      (and ``import subprocess`` -> ``{"subprocess": "subprocess"}``).
    * ``direct_names`` — ``from subprocess import run as r`` ->
      ``{"r": "subprocess.run"}``.

    Only real ``import``/``from`` statements contribute, which is exactly what
    keeps a locally defined ``def run(...)`` out of the map — and therefore out
    of the findings.

    Relative imports (``from .subprocess import run``) are ignored: they name a
    module inside colleague, not the stdlib one.
    """
    module_aliases: dict[str, str] = {}
    direct_names: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import os.path` binds the top-level name `os`.
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                module_aliases[bound] = target
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            for alias in node.names:
                direct_names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    return module_aliases, direct_names


def _resolve_call(func: ast.AST, module_aliases: dict[str, str], direct: dict[str, str]) -> str:
    """Resolve a call target to its dotted stdlib path, or '' if unresolvable."""
    if isinstance(func, ast.Name):
        # A bare call is a spawn only if an import bound that name to one.
        return direct.get(func.id, "")

    if isinstance(func, ast.Attribute):
        dotted = _dotted(func)
        if not dotted:
            return ""
        base, _, rest = dotted.partition(".")
        # An unbound base falls through as itself: `subprocess.run` in a module
        # that never imported subprocess still reads as a spawn. Fail closed —
        # this is a gate.
        return f"{module_aliases.get(base, base)}.{rest}"

    return ""


def scan_file(path: Path, rel: str) -> list[Finding]:
    """Findings for one module. Raises ScanSkipped if it cannot be parsed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ScanSkipped(f"{type(exc).__name__}: {exc}") from exc

    module_aliases, direct = _import_bindings(tree)

    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolve_call(node.func, module_aliases, direct)
        if name not in _SPAWN_CALLS:
            continue
        uses_shell = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        found.append(Finding(module=rel, line=node.lineno, call=name, uses_shell=uses_shell))
    return found


def scan(root: Path) -> Inventory:
    pkg = root / "colleague"
    if not pkg.is_dir():
        raise InventoryError(
            f"{pkg} is not a directory",
            "pass the colleague repo root (the parent of the colleague/ package)",
        )

    inv = Inventory()
    for path in sorted(pkg.rglob("*.py")):
        rel = module_key(path, pkg)
        try:
            findings = scan_file(path, rel)
        except ScanSkipped as exc:
            # Never fail open: an unread file is recorded, not treated as clean.
            inv.skipped[rel] = str(exc)
            continue
        except OSError as exc:
            inv.skipped[rel] = f"{type(exc).__name__}: {exc}"
            continue
        for finding in findings:
            inv.findings.append(finding)
            inv.modules.add(rel)
            if rel not in ALLOWLIST:
                inv.unclassified.append(finding)
    return inv


def head_sha(root: Path) -> str:
    try:
        out = subprocess.run(  # nosec B603,B607 - fixed argv, no shell, git from PATH
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inventory colleague's process-spawn paths.")
    ap.add_argument("root", type=Path, help="path to the colleague repo root")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if an unclassified spawn path exists",
    )
    args = ap.parse_args(argv)

    try:
        inv = scan(args.root)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"hint: {exc.hint}", file=sys.stderr)
        return 2
    except OSError as exc:  # unreadable checkout, broken permissions, …
        print(f"error: cannot read {args.root}: {exc}", file=sys.stderr)
        print("hint: check the checkout exists and is readable", file=sys.stderr)
        return 2

    sha = head_sha(args.root)

    by_profile: dict[str, int] = {}
    for f in inv.findings:
        profile = ALLOWLIST.get(f.module, ("unclassified", False))[0]
        by_profile[profile] = by_profile.get(profile, 0) + 1

    payload = {
        "pinned_sha": PINNED_SHA,
        "observed_sha": sha,
        "sha_matches": sha == PINNED_SHA,
        "spawn_sites": len(inv.findings),
        "modules": len(inv.modules),
        "by_profile": by_profile,
        "shell_true_sites": [f"{f.module}:{f.line}" for f in inv.findings if f.uses_shell],
        "unclassified": [f"{f.module}:{f.line} ({f.call})" for f in inv.unclassified],
        "debt_modules": sorted(inv.debt_modules),
        "debt_remaining": len(inv.debt_modules),
        # A non-empty list means the scan was partial: every count above is a
        # lower bound, not a measurement.
        "skipped": [f"{module} ({reason})" for module, reason in sorted(inv.skipped.items())],
        "skipped_count": len(inv.skipped),
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"colleague inventory @ {sha[:12]} (pinned {PINNED_SHA[:12]})")
        if not payload["sha_matches"]:
            print("  NOTE: checkout differs from the pinned commit; counts may legitimately differ")
        print(f"  spawn sites : {payload['spawn_sites']} across {payload['modules']} modules")
        for profile, count in sorted(by_profile.items()):
            print(f"    {profile:14s}: {count}")
        print(f"  shell=True  : {', '.join(payload['shell_true_sites']) or 'none'}")
        print(f"  debt modules: {payload['debt_remaining']} (must reach 0 by Milestone 3)")
        if inv.unclassified:
            print("  UNCLASSIFIED (fails --check):")
            for entry in payload["unclassified"]:
                print(f"    {entry}")
        if inv.skipped:
            print("  SKIPPED — scan is PARTIAL, counts above are a lower bound:")
            for entry in payload["skipped"]:
                print(f"    {entry}")

    if not args.check:
        return 0

    if inv.skipped:
        # Exit 2, not 1: the scanner could not read the whole checkout, so it
        # has no verdict to give. Reporting "clean" here would be fail-open.
        print(
            f"\nerror: {len(inv.skipped)} file(s) could not be scanned; the inventory is partial",
            file=sys.stderr,
        )
        print(
            "hint: fix or exclude the unparseable file — a partial scan cannot gate anything",
            file=sys.stderr,
        )
        return 2

    if inv.unclassified:
        print(
            f"\nerror: {len(inv.unclassified)} unclassified spawn path(s)",
            file=sys.stderr,
        )
        print(
            "hint: classify the module in ALLOWLIST, or route it through shell-cli",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
