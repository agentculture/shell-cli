#!/usr/bin/env python3
"""Generate byte-pinned colleague-baseline fixtures for shell-cli characterization.

Milestone 0 of the shell-cli build brief
(https://github.com/agentculture/shell-cli/issues/1) requires the six
compatibility tool schemas and their behavioural contract to be captured from
a live colleague checkout, never hand-written. This script is that capture:
it drives colleague's own ``colleague.tools`` module — the real ``SCHEMAS``
list and the real ``ToolExecutor`` handlers — against a pinned commit, and
writes what it observes to ``tests/fixtures/colleague/``.

Usage::

    python3 scripts/capture_colleague_baseline.py /home/spark/git/colleague
    python3 scripts/capture_colleague_baseline.py /path/to/colleague --out tests/fixtures/colleague
    python3 scripts/capture_colleague_baseline.py /path/to/colleague --allow-sha-mismatch

Exit 2 is reserved for an environment error (missing checkout, SHA mismatch,
a capture child that crashed or printed something other than JSON) — the same
convention ``scripts/colleague_inventory.py`` uses, so a broken invocation
never masquerades as a clean run.

WHY THIS RUNS COLLEAGUE IN A CHILD PROCESS
--------------------------------------------
colleague pins exactly one third-party runtime dependency
(``agentfront>=0.20.0`` — see its ``pyproject.toml``). shell-cli's own
process must never import it: ``pyproject.toml`` here declares
``dependencies = []`` and that bar is load-bearing (CLAUDE.md, "The core is
pure-stdlib"). So this script never does ``import colleague`` itself — it
writes a small capture program to a temp file and runs it with colleague's
OWN interpreter (its ``.venv``, or ``uv run --project`` as a fallback), as a
fixed argv vector, never a shell string. Only the JSON it prints on stdout
crosses back into this (pure-stdlib) process.

WHAT GETS WRITTEN
------------------
Three files land in the output directory:

* ``schemas.json`` — ``SCHEMAS[:6]`` verbatim, the contiguous slice colleague
  itself hands to the model (read_file, view_media, write_file, edit_file,
  list_dir, run_command — the 7th entry, ``culture``, is not part of this
  slice and is deliberately excluded).
* ``behavior.json`` — the handler-level captures: line-numbering-before-
  truncation, truncation boundaries, media limits, ``bytes_written``
  accounting, ``list_dir`` shape, and the ``ToolExecutor.execute`` exception
  wrap.
* ``meta.json`` — the observed vs. pinned commit SHA/version.

Every file is serialized with a fixed ``json.dumps`` shape (``indent=2``,
``ensure_ascii=False``, insertion order preserved, trailing newline) so two
runs against the same commit are byte-identical — see
``tests/characterization/test_regeneration_reproducible.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - fixed argv vectors only, no shell strings (control profile)
import sys
import tempfile
from pathlib import Path

# The commit and version this baseline is pinned against. Mirrors
# scripts/colleague_inventory.py's PINNED_SHA/PINNED_VERSION convention.
PINNED_SHA = "28fee290c51fc4310b9fc576981809ad5c3132c6"
PINNED_VERSION = "1.51.0"

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "colleague"

SCHEMAS_FILENAME = "schemas.json"
BEHAVIOR_FILENAME = "behavior.json"
META_FILENAME = "meta.json"

_CAPTURE_TIMEOUT_SECONDS = 120


class GeneratorError(Exception):
    """An environment problem: bad checkout, SHA mismatch, or a broken capture.

    Raised, never printed as a traceback. ``main`` renders it as
    ``error:``/``hint:`` on stderr and returns exit code 2, mirroring
    ``scripts/colleague_inventory.py``'s ``InventoryError``.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# The program that runs INSIDE colleague's own interpreter. Pure stdlib on
# that side too (colleague.tools itself is import-clean beyond `colleague`'s
# own package); its only job is to import the real handlers, exercise them
# against fixed, deterministic inputs, and print ONE JSON document to stdout.
# Nothing here is hand-derived: every captured value is whatever colleague's
# own code actually produced when this ran.
# ---------------------------------------------------------------------------
_CAPTURE_CHILD_SOURCE = r'''
import json
import shutil
import sys
import tempfile
from pathlib import Path

colleague_root = sys.argv[1]
sys.path.insert(0, colleague_root)

from colleague.tools import (  # noqa: E402
    MAX_MEDIA_BYTES,
    SCHEMAS,
    ToolExecutor,
    _LINE_NUMBER_WIDTH,
)
from colleague.config import _DEFAULT_MAX_OUTPUT_CHARS  # noqa: E402


def call(executor, name, arguments):
    """Dispatch one tool call through execute() and capture a neutral shape.

    Never lets a probe's own exception escape and crash the whole capture —
    an unexpected error becomes {"ok": False, ...} data, exactly like every
    other captured outcome, so a single misbehaving probe cannot blank out
    the rest of the baseline.
    """
    try:
        outcome = executor.execute(name, arguments)
        return {
            "ok": True,
            "result": outcome.result,
            "changed_file": outcome.changed_file,
            "media_part": outcome.media_part,
        }
    except Exception as exc:  # noqa: BLE001 - captured as data, not raised
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def fresh_executor(root, **kwargs):
    return ToolExecutor(root, **kwargs)


behaviors = {}

# A UNIQUE work root per run (tempfile.mkdtemp, not a fixed name) -- capture
# may run concurrently (e.g. two characterization tests regenerating in
# parallel xdist workers), and a shared fixed path would race. The random
# directory name inevitably leaks into a couple of error strings (e.g.
# media.validate_attachment's ValueError embeds the resolved absolute path),
# so every captured string is redacted below before this prints its JSON --
# that keeps the WORKSPACE concurrency-safe while keeping the FIXTURE
# byte-for-byte reproducible across runs.
root = Path(tempfile.mkdtemp(prefix="shell_cli_colleague_baseline_capture_"))
_ROOT_STR = str(root.resolve())
_ROOT_PLACEHOLDER = "<capture-workspace>"
try:
    # -- read_file: numbering happens BEFORE truncation (issue #240 fix) ----
    # 1000 fixed-format lines, comfortably over the 25000-char output cap
    # once numbered. The captured `result` is the load-bearing fixture: a
    # reimplementation that truncates first and numbers second produces a
    # DIFFERENT string (a phantom numbered truncation-marker line, and a
    # final length that overshoots the cap) — see
    # tests/characterization/test_read_file_ordering.py.
    lines = [
        f"line{i:05d}_payload_padding_xxxxxxxxxxxxxxxxxxxxxxxxx"
        for i in range(1, 1001)
    ]
    big_content = "\n".join(lines) + "\n"
    (root / "big.txt").write_text(big_content, encoding="utf-8")
    ex = fresh_executor(root, max_output_chars=_DEFAULT_MAX_OUTPUT_CHARS)
    behaviors["read_file_numbers_then_truncates"] = {
        "input_text": big_content,
        "max_output_chars": _DEFAULT_MAX_OUTPUT_CHARS,
        "line_number_width": _LINE_NUMBER_WIDTH,
        **call(ex, "read_file", {"path": "big.txt"}),
    }

    # -- read_file: recoverable model-visible errors -------------------------
    ex = fresh_executor(root)
    behaviors["read_file_missing_required_path"] = call(ex, "read_file", {})

    ex = fresh_executor(root)
    behaviors["read_file_path_escape"] = call(
        ex, "read_file", {"path": "../../../etc/passwd"}
    )

    ex = fresh_executor(root)
    behaviors["read_file_no_such_file"] = call(
        ex, "read_file", {"path": "does/not/exist.txt"}
    )

    # -- write_file: bytes_written is the FULL content written ---------------
    ex = fresh_executor(root)
    outcome = call(ex, "write_file", {"path": "w.txt", "content": "hello world"})
    outcome["bytes_written"] = ex.bytes_written
    outcome["changed"] = sorted(ex.changed)
    behaviors["write_file_bytes_written"] = outcome

    ex = fresh_executor(root)
    outcome = call(
        ex, "write_file", {"path": "nested/deep/file.txt", "content": "hi"}
    )
    outcome["bytes_written"] = ex.bytes_written
    outcome["changed"] = sorted(ex.changed)
    behaviors["write_file_creates_nested_dirs"] = outcome

    # -- edit_file: bytes_written is ONLY the replacement bytes --------------
    (root / "e.txt").write_text("AAAA BBBB CCCC", encoding="utf-8")
    ex = fresh_executor(root)
    outcome = call(
        ex, "edit_file", {"path": "e.txt", "old_string": "BBBB", "new_string": "XY"}
    )
    outcome["bytes_written"] = ex.bytes_written
    behaviors["edit_file_bytes_written_single"] = outcome

    (root / "r.txt").write_text("XX YY XX YY XX", encoding="utf-8")
    ex = fresh_executor(root)
    outcome = call(
        ex,
        "edit_file",
        {
            "path": "r.txt",
            "old_string": "XX",
            "new_string": "Q",
            "replace_all": True,
        },
    )
    outcome["bytes_written"] = ex.bytes_written
    behaviors["edit_file_bytes_written_replace_all"] = outcome

    ex = fresh_executor(root)
    behaviors["edit_file_old_string_not_found"] = call(
        ex, "edit_file", {"path": "e.txt", "old_string": "nope", "new_string": "x"}
    )

    (root / "amb.txt").write_text("XX YY XX", encoding="utf-8")
    ex = fresh_executor(root)
    behaviors["edit_file_ambiguous_without_replace_all"] = call(
        ex, "edit_file", {"path": "amb.txt", "old_string": "XX", "new_string": "Z"}
    )

    # -- list_dir: sorted names, trailing slash on directories ---------------
    (root / "dirtest").mkdir()
    (root / "dirtest" / "b.txt").write_text("x", encoding="utf-8")
    (root / "dirtest" / "a.txt").write_text("x", encoding="utf-8")
    (root / "dirtest" / "sub").mkdir()
    ex = fresh_executor(root)
    behaviors["list_dir_shape"] = call(ex, "list_dir", {"path": "dirtest"})

    ex = fresh_executor(root)
    behaviors["list_dir_not_a_directory"] = call(
        ex, "list_dir", {"path": "e.txt"}
    )

    # -- run_command: the exact truncation boundary ---------------------------
    # The formatted result is f"exit={code}\n{stdout}{stderr}"; the 7-char
    # "exit=0\n" prefix counts toward the 25000-char cap, so the command
    # output length that lands exactly on the boundary is cap - 7.
    prefix_len = len("exit=0\n")
    boundary = _DEFAULT_MAX_OUTPUT_CHARS - prefix_len
    ex = fresh_executor(root)
    behaviors["run_command_truncation_boundary_not_truncated"] = call(
        ex,
        "run_command",
        {"command": f"python3 -c \"import sys; sys.stdout.write('a'*{boundary})\""},
    )
    ex = fresh_executor(root)
    behaviors["run_command_truncation_boundary_truncated"] = call(
        ex,
        "run_command",
        {
            "command": (
                f"python3 -c \"import sys; sys.stdout.write('a'*{boundary + 1})\""
            )
        },
    )

    # -- run_command: exit code + stdout/stderr concatenation shape ----------
    ex = fresh_executor(root)
    behaviors["run_command_exit_code_and_body_shape"] = call(
        ex,
        "run_command",
        {
            "command": (
                "python3 -c \"import sys; print('out-line'); "
                "print('err-line', file=sys.stderr); sys.exit(3)\""
            )
        },
    )

    ex = fresh_executor(root)
    behaviors["run_command_missing_required_command"] = call(
        ex, "run_command", {}
    )

    # -- view_media: images only, size-capped -----------------------------
    (root / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    ex = fresh_executor(root)
    behaviors["view_media_ok"] = call(ex, "view_media", {"path": "img.png"})

    (root / "snd.wav").write_bytes(b"RIFF" + b"0" * 50)
    ex = fresh_executor(root)
    behaviors["view_media_audio_rejected"] = call(
        ex, "view_media", {"path": "snd.wav"}
    )

    (root / "big.png").write_bytes(b"0" * (MAX_MEDIA_BYTES + 1))
    ex = fresh_executor(root)
    behaviors["view_media_oversize_rejected"] = call(
        ex, "view_media", {"path": "big.png"}
    )

    (root / "file.xyz").write_bytes(b"hello")
    ex = fresh_executor(root)
    behaviors["view_media_unknown_extension_rejected"] = call(
        ex, "view_media", {"path": "file.xyz"}
    )

    # -- ToolExecutor.execute: wraps every non-ToolError exception -----------
    # A NUL byte in a path makes Path.resolve() raise a bare ValueError deep
    # inside _safe_path — a MODEL-visible malformed argument, not a harness
    # bug. Captured twice: once through execute() (wrapped, recoverable),
    # once by calling the handler directly (the raw, unwrapped exception the
    # wrap exists to prevent from escaping and aborting the whole drive).
    ex = fresh_executor(root)
    behaviors["execute_wraps_non_tool_error"] = call(
        ex, "read_file", {"path": "a\x00b"}
    )
    ex = fresh_executor(root)
    try:
        ex._read_file({"path": "a\x00b"})
        raw = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - the point is to capture the raw shape
        raw = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    behaviors["execute_wraps_non_tool_error"]["raw_handler_exception"] = raw

    # -- ToolExecutor.execute: unknown tool name ------------------------------
    ex = fresh_executor(root)
    behaviors["execute_unknown_tool"] = call(ex, "no_such_tool", {})
finally:
    shutil.rmtree(root, ignore_errors=True)

def _redact_workspace_path(value):
    """Replace the random capture-workspace path with a fixed placeholder.

    Recurses through the captured payload so ANY string that happened to
    embed the resolved absolute root -- today just
    media.validate_attachment's "unknown extension" ValueError -- is
    normalized before it is serialized, keeping the fixture reproducible
    even though the workspace itself must be a fresh random directory
    (see the concurrency note above `root = Path(tempfile.mkdtemp(...))`).
    """
    if isinstance(value, str):
        return value.replace(_ROOT_STR, _ROOT_PLACEHOLDER)
    if isinstance(value, dict):
        return {k: _redact_workspace_path(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_workspace_path(v) for v in value]
    return value


payload = {
    "schemas": SCHEMAS[:6],
    "constants": {
        "max_output_chars": _DEFAULT_MAX_OUTPUT_CHARS,
        "max_media_bytes": MAX_MEDIA_BYTES,
        "line_number_width": _LINE_NUMBER_WIDTH,
    },
    "behaviors": _redact_workspace_path(behaviors),
}

sys.stdout.write(json.dumps(payload))
'''


def _head_sha(root: Path) -> str:
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


def colleague_interpreter(colleague_root: Path) -> list[str]:
    """argv PREFIX that runs a script inside colleague's OWN interpreter.

    Prefers colleague's own ``.venv`` (fast, no resolver work); falls back to
    ``uv run --project`` so a checkout without a materialized venv still
    works. Either way this returns a fixed argv vector — never a shell
    string — matching the ``control`` profile shell-cli documents for
    trusted-CLI invocations.
    """
    venv_python = colleague_root / ".venv" / "bin" / "python3"
    if venv_python.is_file():
        return [str(venv_python)]
    return ["uv", "run", "--project", str(colleague_root), "python3"]


def run_capture(colleague_root: Path) -> dict:
    """Run the capture child against *colleague_root* and return its payload."""
    if not (colleague_root / "colleague").is_dir():
        raise GeneratorError(
            f"{colleague_root} does not contain a colleague/ package",
            "pass the colleague repo root (the parent of the colleague/ package)",
        )

    interpreter = colleague_interpreter(colleague_root)
    with tempfile.TemporaryDirectory() as td:
        script_path = Path(td) / "_colleague_capture_child.py"
        script_path.write_text(_CAPTURE_CHILD_SOURCE, encoding="utf-8")
        argv = interpreter + [str(script_path), str(colleague_root)]
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=_CAPTURE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GeneratorError(
                f"failed to launch colleague's interpreter ({' '.join(interpreter)}): {exc}",
                "check the colleague checkout has a .venv (uv sync) or uv is on PATH",
            ) from exc

        if proc.returncode != 0:
            raise GeneratorError(
                f"capture child exited {proc.returncode}",
                f"stderr: {proc.stderr.strip() or '<empty>'}",
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise GeneratorError(
                f"capture child did not print valid JSON: {exc}",
                f"stderr: {proc.stderr.strip() or '<empty>'}",
            ) from exc


def generate(
    colleague_root: Path,
    out_dir: Path,
    *,
    require_pinned_sha: bool = True,
) -> dict[str, Path]:
    """Capture the baseline and write it to *out_dir*. Returns {filename: path}."""
    observed_sha = _head_sha(colleague_root)
    if require_pinned_sha and observed_sha != PINNED_SHA:
        raise GeneratorError(
            f"colleague checkout at {colleague_root} is at {observed_sha or 'unknown'}, "
            f"not the pinned {PINNED_SHA}",
            "pass --allow-sha-mismatch to capture anyway (the baseline will record the "
            "mismatch), or point at a checkout of the pinned commit",
        )

    payload = run_capture(colleague_root)
    if "schemas" not in payload or "behaviors" not in payload:
        raise GeneratorError(
            "capture child returned an unexpected payload shape",
            "expected top-level 'schemas' and 'behaviors' keys",
        )

    schemas = payload["schemas"]
    behavior = {"constants": payload.get("constants", {}), "behaviors": payload["behaviors"]}
    meta = {
        "pinned_sha": PINNED_SHA,
        "pinned_version": PINNED_VERSION,
        "observed_sha": observed_sha,
        "sha_matches": observed_sha == PINNED_SHA,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for filename, data in (
        (SCHEMAS_FILENAME, schemas),
        (BEHAVIOR_FILENAME, behavior),
        (META_FILENAME, meta),
    ):
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        path = out_dir / filename
        path.write_text(text, encoding="utf-8")
        written[filename] = path

    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("colleague_root", type=Path, help="path to the colleague repo root")
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="fixtures output directory (default: tests/fixtures/colleague)",
    )
    ap.add_argument(
        "--allow-sha-mismatch",
        action="store_true",
        help="capture even when the checkout is not at the pinned commit",
    )
    args = ap.parse_args(argv)

    try:
        written = generate(
            args.colleague_root,
            args.out,
            require_pinned_sha=not args.allow_sha_mismatch,
        )
    except GeneratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"hint: {exc.hint}", file=sys.stderr)
        return 2

    for filename, path in written.items():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
