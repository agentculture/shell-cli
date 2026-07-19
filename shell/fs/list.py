"""``fs.list``: list a directory's entries, confined to the environment's work root.

Ported from colleague's ``ToolExecutor._list_dir`` (``colleague/tools.py:947-952``,
pinned commit ``28fee290c51fc4310b9fc576981809ad5c3132c6``). Entries are sorted
names with a trailing ``/`` on directories; a target that is not a directory --
including one that does not exist at all, since ``Path.is_dir()`` answers
``False`` for both -- is refused with the same ``not a directory: <path>``
message colleague produces, deliberately not distinguished into two error
shapes colleague itself never distinguished.

Shares :func:`shell.fs.read._safe_path`'s confinement semantics but does not
import it -- see ``shell/fs/__init__.py``: this package stays an empty marker,
consumers import each handler module by its explicit path, and the small
confinement/truncation helpers are intentionally re-ported here rather than
factored into a shared internal module that no consumer has asked for yet.

The same non-claim applies as in :mod:`shell.fs.read`: confinement to the work
root is real, but it is not process isolation, and ``fs.list`` never spawns a
process in the first place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.results import Effects, OperationResult, OperationStatus

__all__ = ["KIND"]

#: The operation kind this module registers.
KIND = "fs.list"


class _ListError(Exception):
    """A recoverable, model-visible ``fs.list`` failure.

    Raised only for the error cases this module explicitly models (path
    escape, target is not a directory). Caught in :func:`_list` and turned
    into a ``FAILED`` result. A genuinely unexpected exception -- e.g. a NUL
    byte in the path raising a bare ``ValueError`` out of ``Path.resolve()`` --
    is deliberately left to propagate, so ``shell.operations.execute``'s
    handler-crash wrap is what catches it, exactly as it would for any other
    handler.
    """


def _truncate(text: str, limit: int) -> str:
    """Bound *text* to *limit* characters, appending a truncation marker.

    Ported verbatim from colleague's ``ToolExecutor._truncate``
    (``tools.py:724-728``).
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} chars]"


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve *rel* under *root*, refusing any path that escapes it.

    Ported from colleague's ``_safe_path`` (``tools.py:730-735``) unchanged --
    see ``shell.fs.read._safe_path`` for the full rationale, including why a
    symlink inside *root* pointing outside it is caught (``Path.resolve()``
    follows symlinks before the containment check runs) and why a malformed
    path's ``ValueError`` is left uncaught here.
    """
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise _ListError(f"path '{rel}' escapes the work root")
    return candidate


def _target_path(arguments: Mapping[str, Any]) -> str:
    """The directory to list, defaulting to the work root itself.

    Mirrors colleague's ``arguments.get("path", ".")`` (``tools.py:948``):
    unlike ``read_file``, ``path`` is optional for ``list_dir``.
    """
    return str(arguments.get("path", "."))


def _list(operation: Operation, environment: Environment) -> OperationResult:
    try:
        rel = _target_path(operation.arguments)
        path = _safe_path(environment.work_root, rel)
        if not path.is_dir():
            raise _ListError(f"not a directory: {rel}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    except _ListError as exc:
        message = str(exc)
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.FAILED,
            error=message,
            rendering=message,
            effects=Effects(complete=True),
        )

    limit = operation.resolved_max_output_bytes(environment)
    joined = "\n".join(entries)
    rendering = _truncate(joined, limit)

    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"path": rel, "entries": entries, "truncated": rendering != joined},
        rendering=rendering,
        # An observation is not a mutation: it changed nothing, and that empty
        # effect list is complete by construction, not merely unclaimed.
        effects=Effects(complete=True),
    )


register(KIND, intent=OperationIntent.OBSERVE, default_profile=ExecutionProfile.OBSERVE, run=_list)
