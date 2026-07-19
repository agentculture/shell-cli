#!/usr/bin/env python3
"""Generate source-code hashes for the six colleague tool handlers.

Milestone 1 of shell-cli extracts six handlers from colleague's ToolExecutor
(``_read_file``, ``_view_media``, ``_write_file``, ``_edit_file``, ``_list_dir``,
``_run_command``). This script pins their source at the extraction baseline commit
so a change to either copy during the migration window fails CI.

The hashes are **generated from colleague at the pinned commit**, never
hand-maintained. This script is the authority; ``scripts/handler_hashes.json``
is its output.

Pure stdlib, read-only, AST-based. Hashes source segments extracted by
``ast.get_source_segment``, not the whole file — so unrelated edits to
``tools.py`` do not produce false alarms.

What this gate does and does not catch:
- DOES: detect changes to the six named handlers' source text at a pinned baseline.
- DOES NOT: detect behavioural change reached through a helper the handlers call.
- DOES NOT: detect anything outside those six functions.

Usage::

    python3 scripts/handler_hashes.py /home/spark/git/colleague
    python3 scripts/handler_hashes.py /path/to/colleague --json > scripts/handler_hashes.json

Exit codes:
- 0: success
- 1: bad arguments / no handlers found
- 2: environment error (checkout missing, unreadable, or syntax error)
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess  # nosec B404 - reads git metadata from a local checkout
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# The commit this baseline was recorded at. If the checkout is not at this SHA,
# fail LOUDLY — a drift gate that silently skips when it cannot find its baseline
# is worthless.
PINNED_SHA = "28fee290c51fc4310b9fc576981809ad5c3132c6"
PINNED_VERSION = "1.51.0"

# The six tool handlers to be extracted into shell-cli.
HANDLER_NAMES = {
    "_read_file",
    "_view_media",
    "_write_file",
    "_edit_file",
    "_list_dir",
    "_run_command",
}


class HashError(Exception):
    """An environment or structural problem: checkout missing, unreadable, syntax error, etc.

    Raised, never printed as a traceback. ``main`` renders it as
    ``error:``/``hint:`` on stderr and returns exit code 2.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class HandlerHash:
    """Record of one handler's hash at the pinned baseline."""

    name: str
    sha256: str


def head_sha(root: Path) -> str:
    """Get HEAD commit SHA from a git checkout, or 'unknown' if unavailable."""
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


def hash_source(source: str) -> str:
    """SHA256 hash of source code."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def extract_handlers(tools_path: Path) -> dict[str, str]:
    """Extract the six handlers and return {name -> source_segment}.

    Raises HashError if the file cannot be read, parsed, or a handler is missing.
    """
    try:
        source = tools_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HashError(
            f"{tools_path} not found",
            "pass the colleague repo root (the parent of the colleague/ package)",
        ) from exc
    except (UnicodeDecodeError, OSError) as exc:
        raise HashError(
            f"cannot read {tools_path}: {exc}",
            "check the file is readable and contains valid UTF-8",
        ) from exc

    try:
        tree = ast.parse(source, filename=str(tools_path))
    except SyntaxError as exc:
        raise HashError(
            f"syntax error in {tools_path}: {exc}",
            "the checkout may be corrupted",
        ) from exc

    handlers: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in HANDLER_NAMES:
            continue
        # Extract the source segment for this function.
        seg = ast.get_source_segment(source, node)
        if seg is None:
            raise HashError(
                f"cannot extract source segment for {node.name}",
                "the AST walker may be misaligned with the source",
            )
        handlers[node.name] = seg

    # Fail closed: every handler must be found.
    missing = HANDLER_NAMES - set(handlers.keys())
    if missing:
        raise HashError(
            f"handler(s) not found: {', '.join(sorted(missing))}",
            "the checkout may be at a different commit, or the handlers were renamed",
        )

    return handlers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate SHA256 hashes of the six colleague tool handlers."
    )
    ap.add_argument("root", type=Path, help="path to the colleague repo root")
    ap.add_argument(
        "--json",
        action="store_true",
        help="output as JSON (written to stdout)",
    )
    ap.add_argument(
        "--skip-sha-check",
        action="store_true",
        help="skip SHA verification (for testing only)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="compare against baseline hashes; exit 1 if any mismatch",
    )
    args = ap.parse_args(argv)

    tools_path = args.root / "colleague" / "tools.py"

    # Fail closed: the SHA must match. A drift gate that silently proceeds when it
    # cannot find its baseline is worthless. (Skip for unit tests.)
    if not args.skip_sha_check:
        observed_sha = head_sha(args.root)
        if observed_sha != PINNED_SHA:
            print("error: checkout SHA mismatch", file=sys.stderr)
            print(
                f"hint: pinned {PINNED_SHA[:12]}, observed {observed_sha[:12]}; "
                f"drift gate requires an exact match",
                file=sys.stderr,
            )
            return 2

    # Extract and hash the handlers.
    try:
        handlers = extract_handlers(tools_path)
    except HashError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"hint: {exc.hint}", file=sys.stderr)
        return 2

    # Build output.
    records = [
        HandlerHash(name=name, sha256=hash_source(handlers[name])) for name in sorted(HANDLER_NAMES)
    ]

    payload = {
        "pinned_sha": PINNED_SHA,
        "pinned_version": PINNED_VERSION,
        "handlers": [asdict(r) for r in records],
    }

    if args.check:
        # Load baseline and compare.
        baseline_path = Path(__file__).parent / "handler_hashes.json"
        if not baseline_path.exists():
            print(
                f"error: baseline hashes not found at {baseline_path}",
                file=sys.stderr,
            )
            print("hint: run without --check to generate hashes", file=sys.stderr)
            return 2

        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_records = {h["name"]: h for h in baseline_data.get("handlers", [])}

        mismatches = []
        for record in records:
            baseline_hash = baseline_records.get(record.name, {}).get("sha256")
            if not baseline_hash:
                mismatches.append(f"{record.name}: not in baseline")
            elif record.sha256 != baseline_hash:
                mismatches.append(
                    f"{record.name}: hash mismatch "
                    f"(baseline {baseline_hash[:16]}..., "
                    f"observed {record.sha256[:16]}...)"
                )

        if mismatches:
            print("error: handler source mismatch", file=sys.stderr)
            for mismatch in mismatches:
                print(f"  {mismatch}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"colleague handlers @ {PINNED_SHA[:12]} (version {PINNED_VERSION})")
        for record in records:
            print(f"  {record.name:20s}: {record.sha256[:16]}...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
