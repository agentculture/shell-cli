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

``--check`` exits 1 when an unclassified spawn path exists, which is what CI
runs as a known-debt gate. Exit 2 is reserved for an environment error (a
missing or unreadable checkout) so CI can distinguish "a new unclassified path
landed" from "the clone is broken" — a gate that silently no-ops is worse than
no gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess  # nosec B404 - reads git metadata from a local checkout
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The commit this inventory was recorded against. Reproducing the numbers on a
# different commit is expected to differ — that is the point of pinning.
PINNED_SHA = "28fee290c51fc4310b9fc576981809ad5c3132c6"
PINNED_VERSION = "1.51.0"

# Callables that create a process. Matched on the dotted attribute path, so
# ``subprocess.run`` matches and a local variable named ``run`` does not.
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

    @property
    def debt_modules(self) -> set[str]:
        return {m for m in self.modules if ALLOWLIST.get(m, ("", False))[1]}


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


def scan_file(path: Path, rel: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
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
        rel = str(path.relative_to(pkg))
        for finding in scan_file(path, rel):
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

    if args.check and inv.unclassified:
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
