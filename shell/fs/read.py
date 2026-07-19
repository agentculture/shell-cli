"""``fs.read``: read a UTF-8 text file, confined to the environment's work root.

Ported from colleague's ``ToolExecutor._read_file`` (``colleague/tools.py:813``,
pinned commit ``28fee290c51fc4310b9fc576981809ad5c3132c6``), which this module
must stay behaviourally identical to. Two properties of that port are
load-bearing and are pinned by ``tests/test_fs_read.py`` rather than merely
described here:

**Numbering precedes truncation.** :func:`_number_lines` runs on the full file
text before :func:`_truncate` ever sees it. Reversing the order is the exact
regression colleague issue #240 recorded: a served model cited a line number
~240 off because the text had been windowed *before* it was numbered, so the
surviving lines were renumbered from 1 rather than carrying their true file
position. Truncation only ever drops the tail of an already-numbered string; it
never renumbers what remains.

**Confinement is real but is not process isolation.** :func:`_safe_path`
resolves the requested path against :attr:`~shell.environment.Environment.work_root`
and refuses anything that lands outside it -- including a symlink *inside* the
root that points *outside* it, because ``Path.resolve()`` follows symlinks
before the containment check runs. That confinement is a genuine guarantee of
this handler. It says nothing about process isolation: ``fs.read`` never spawns
a process, and the runner's isolation posture (``"none"`` for :class:`HostRunner`)
is an orthogonal claim recorded separately on every result's evidence.

A handler crash on malformed input -- e.g. a NUL byte in the path, which makes
``Path.resolve()`` raise a bare ``ValueError`` deep inside :func:`_safe_path` --
is deliberately left uncaught here. It is not one of this module's modelled
error cases, so it propagates out of :func:`_read` and is converted into a
recoverable ``FAILED`` result by ``shell.operations.execute``'s handler-crash
wrap (mirroring ``ToolExecutor.execute``, ``colleague/tools.py:788-800``), never
an aborted run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.results import Effects, OperationResult, OperationStatus

__all__ = ["KIND"]

#: The operation kind this module registers.
KIND = "fs.read"

#: Column width for the ``cat -n`` style line-number prefix. Matches colleague's
#: ``_LINE_NUMBER_WIDTH`` (``tools.py:596``) -- GNU ``cat -n``'s default
#: right-justified 6-column number.
_LINE_NUMBER_WIDTH = 6


class _ReadError(Exception):
    """A recoverable, model-visible ``fs.read`` failure.

    Raised only for the error cases this module explicitly models (missing
    argument, path escape, missing file, unreadable file). Caught in
    :func:`_read` and turned into a ``FAILED`` result. Anything else --
    a genuinely unexpected exception -- is deliberately left to propagate, so
    that ``shell.operations.execute``'s handler-crash wrap is the thing that
    catches it, exactly as it would for any other handler.
    """


def _number_lines(text: str) -> str:
    """Ground *text* for citation: prefix every real line with its true line number.

    ``cat -n`` style -- ``f"{n:6d}\\t{line}"`` -- so a model quoting a result
    line is quoting a copy-derived ``file:line``, never a re-counted one.
    Ported verbatim from colleague's ``_number_lines`` (``tools.py:599-621``),
    including its choice to split on bare ``"\\n"`` only, NOT
    :meth:`str.splitlines`, which also breaks on ``\\v``/``\\f``/``\\x1c``-``\\x1e``/
    ``\\x85``/``\\u2028``/``\\u2029`` -- a wider set that would silently invent
    phantom line boundaries a real ``grep -n``/editor would never count. A
    trailing newline terminates the last line without minting a phantom extra
    line (the same convention as ``cat -n``/``grep -n``); an empty file grounds
    to an empty string (no lines to number).

    Display-only: the numbering is never written to disk.
    """
    if text == "":
        return ""
    body = text[:-1] if text.endswith("\n") else text
    lines = body.split("\n")
    return "\n".join(f"{i:{_LINE_NUMBER_WIDTH}d}\t{line}" for i, line in enumerate(lines, start=1))


def _truncate(text: str, limit: int) -> str:
    """Bound *text* to *limit* characters, appending a truncation marker.

    Ported verbatim from colleague's ``ToolExecutor._truncate``
    (``tools.py:724-728``). Applied AFTER :func:`_number_lines`, never before --
    see the module docstring.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} chars]"


def _require_path(arguments: Mapping[str, Any]) -> str:
    """Fetch the required ``path`` argument or raise a self-correcting error.

    Mirrors colleague's ``_require`` (``tools.py:624-637``): a missing required
    argument is a model error, not a harness bug, and must cost one
    model-visible ``FAILED`` step rather than an unhandled ``KeyError``.
    """
    if "path" not in arguments:
        raise _ReadError("fs.read requires 'path'")
    return str(arguments["path"])


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve *rel* under *root*, refusing any path that escapes it.

    Ported from colleague's ``_safe_path`` (``tools.py:730-735``) unchanged.
    ``Path.resolve()`` follows symlinks that exist on disk before this function
    ever compares the result to *root*, so a symlink *inside* the root that
    points *outside* it is caught by the same identity/ancestry check as a
    literal ``../`` escape -- confinement is not merely a string check on the
    literal path.

    A malformed path -- e.g. one containing an embedded NUL byte -- makes
    ``.resolve()`` raise a bare ``ValueError`` before this function's own logic
    runs. That is deliberately NOT caught here; see the module docstring.
    """
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise _ReadError(f"path '{rel}' escapes the work root")
    return candidate


def _read(operation: Operation, environment: Environment) -> OperationResult:
    try:
        rel = _require_path(operation.arguments)
        path = _safe_path(environment.work_root, rel)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise _ReadError(f"no such file: {rel}") from exc
        except OSError as exc:
            raise _ReadError(f"cannot read {rel}: {exc}") from exc
    except _ReadError as exc:
        message = str(exc)
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.FAILED,
            error=message,
            rendering=message,
            # A read that never touched the filesystem successfully changed
            # nothing -- the (empty) effect list is exhaustively known, not a
            # partial view of a mutation that may have half-happened.
            effects=Effects(complete=True),
        )

    limit = operation.resolved_max_output_bytes(environment)
    numbered = _number_lines(text)
    rendering = _truncate(numbered, limit)

    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"path": rel, "truncated": rendering != numbered},
        rendering=rendering,
        # An observation is not a mutation: it changed nothing, and that empty
        # effect list is complete by construction, not merely unclaimed.
        effects=Effects(complete=True),
    )


register(KIND, intent=OperationIntent.OBSERVE, default_profile=ExecutionProfile.OBSERVE, run=_read)
