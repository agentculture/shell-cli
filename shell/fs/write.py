"""``fs.write`` and ``fs.edit``: confined file mutation with byte accounting.

Ported from colleague's ``_write_file`` / ``_edit_file``
(``colleague/tools.py:860-945``, pinned SHA ``28fee29``) by reading source, not
by importing colleague — see ``tests/test_boundaries.py``. Three of colleague's
behaviours travel here verbatim because a downstream consumer's ROI accounting
depends on getting them exactly right, not approximately right:

* **``write`` accounts the FULL content**; ``edit`` accounts ONLY the
  replacement bytes (occurrences replaced times ``len(new_string)``), never the
  whole file. This is colleague's honest cost-of-output signal
  (``tools.py:872-875`` and ``:938-941``) and is pinned by the committed
  characterization fixture (``tests/fixtures/colleague/behavior.json``,
  ``write_file_bytes_written`` / ``edit_file_bytes_written_single`` /
  ``edit_file_bytes_written_replace_all``).
* ``newline=""`` on every write disables newline translation so the on-disk
  byte count matches ``len(content.encode("utf-8"))`` on every platform
  (``tools.py:866-870``).
* A known, expected failure (missing argument, path escape, no such file,
  non-UTF-8 file, ``old_string`` not found or ambiguous) becomes a ``FAILED``
  :class:`~shell.results.OperationResult`, never a raised exception — the
  model-visible, recoverable-step contract colleague's ``ToolError`` exists for
  (``tools.py:672-675``).

Two confinement mechanisms apply, and they are deliberately kept distinct
rather than folded into one check:

* **Root escape** (this module's :func:`_safe_path`, porting ``_safe_path`` at
  ``tools.py:730``) refuses any target that resolves outside
  :attr:`~shell.environment.Environment.work_root` — ``..`` traversal and a
  symlink whose target lands outside the root alike, because
  :meth:`pathlib.Path.resolve` follows symlinks in every path component that
  exists before comparing against the root. A refusal here is a ``FAILED``
  result: the request itself is malformed.
* **Read-only subtree confinement** (:func:`shell.policy.check_write`)
  generalises colleague's hard-coded neighbour-clone guard
  (``_refuse_clone_write``, ``tools.py:737``) into
  :attr:`~shell.environment.Environment.read_only_paths`. A refusal here is a
  ``DENIED`` result carrying the :class:`~shell.results.PolicyVerdict`
  :func:`~shell.policy.check_write` returned.

  This is **not** the operator-approvals policy (:meth:`shell.policy.Policy.
  check_run_command`) — that gate has no jurisdiction over structured
  filesystem operations at all (see :func:`shell.operations.GATED_KIND_PREFIXES`
  and ``tests/test_loop_run_command_policy.py:366-386`` on the colleague side).
  :func:`shell.operations.execute` always overwrites the *returned* verdict
  with the outer ``run_command`` gate's verdict (``UNGATED`` for any ``fs.*``
  kind) on the way out, so the check-write reason is carried on ``error`` /
  ``rendering`` instead of surviving on the final ``verdict`` field. That is
  the outer pipeline's existing, already-tested behaviour, not something this
  module can or should change.

Both kinds declare ``intent=MUTATE``, so :func:`shell.operations.execute`
previews them unless the caller passes ``apply=True`` — this module adds no
handler-level preview rendering that predicts a diff; the pipeline's own
preview branch (``Effects(complete=False)``, never reached with these
handlers) already covers that honestly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.policy import check_write
from shell.results import Effects, OperationResult, OperationStatus, PolicyDecision

__all__: tuple[str, ...] = ()

WRITE_KIND = "fs.write"
EDIT_KIND = "fs.edit"


class _FsError(Exception):
    """A known, recoverable ``fs.write`` / ``fs.edit`` argument or path error.

    Caught inside the handler and turned into a ``FAILED``
    :class:`~shell.results.OperationResult` with an informative message —
    never allowed to escape the handler and be rewrapped by the pipeline's
    generic crash message. The internal analogue of colleague's ``ToolError``.
    """


def _require(arguments: Mapping[str, Any], key: str, kind: str) -> Any:
    """Fetch a required argument or raise a self-describing :class:`_FsError`.

    Ports colleague's ``_require`` (``tools.py:624-637``): a missing argument
    is a model error, not a harness bug, and must cost one recoverable step,
    never the run.
    """
    if key not in arguments:
        raise _FsError(f"{kind} requires '{key}'")
    return arguments[key]


def _safe_path(environment: Environment, rel: str) -> Path:
    """Resolve *rel* under the work root, refusing anything that escapes it.

    Ports ``_safe_path`` (``tools.py:730-735``), rooted at
    :attr:`~shell.environment.Environment.work_root` instead of colleague's
    single fixed repo root. :meth:`~pathlib.Path.resolve` follows symlinks in
    every already-existing path component before the comparison runs, so a
    symlink inside the work root that targets somewhere else is refused
    exactly like a literal ``..`` traversal — both simply resolve outside the
    root and fail the same containment check.
    """
    root = Path(environment.work_root)
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise _FsError(f"path {rel!r} escapes the confined root")
    return candidate


def _failed(operation: Operation, message: str) -> OperationResult:
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.FAILED,
        error=message,
        rendering=message,
    )


def _resolve_target(
    operation: Operation, environment: Environment, rel: str
) -> tuple[Path | None, OperationResult | None]:
    """Resolve *rel* and apply both confinement mechanisms.

    Returns ``(path, None)`` when the target may be written, or ``(None,
    result)`` with the ready-made terminal result to return otherwise — a
    ``FAILED`` result for a root escape, a ``DENIED`` result carrying
    :func:`shell.policy.check_write`'s verdict for a declared read-only
    subtree.
    """
    try:
        path = _safe_path(environment, rel)
    except _FsError as exc:
        return None, _failed(operation, str(exc))

    verdict = check_write(path, environment)
    if verdict.decision is PolicyDecision.DENIED:
        return None, OperationResult(
            operation_id=operation.id,
            status=OperationStatus.DENIED,
            verdict=verdict,
            error=verdict.reason,
            rendering=verdict.reason,
        )

    return path, None


def _write(operation: Operation, environment: Environment) -> OperationResult:
    arguments = operation.arguments
    try:
        rel = str(_require(arguments, "path", WRITE_KIND))
    except _FsError as exc:
        return _failed(operation, str(exc))
    content = str(arguments.get("content", ""))

    path, refusal = _resolve_target(operation, environment, rel)
    if refusal is not None:
        return refusal

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # newline="" disables newline translation so the on-disk byte count
        # equals len(content.encode("utf-8")) on every platform (tools.py:866-870).
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        return _failed(operation, f"cannot write {rel}: {exc}")

    n_bytes = len(content.encode("utf-8"))
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"path": rel, "bytes_written": n_bytes},
        rendering=f"wrote {n_bytes} bytes to {rel}",
        # A single fs.write handler genuinely knows its one and only effect —
        # complete=True is an earned claim here, not the honest-default False.
        effects=Effects(changed_paths=(rel,), bytes_written=n_bytes, complete=True),
    )


def _edit(operation: Operation, environment: Environment) -> OperationResult:
    arguments = operation.arguments
    try:
        rel = str(_require(arguments, "path", EDIT_KIND))
        old = str(_require(arguments, "old_string", EDIT_KIND))
        new = str(_require(arguments, "new_string", EDIT_KIND))
    except _FsError as exc:
        return _failed(operation, str(exc))
    replace_all = bool(arguments.get("replace_all", False))

    path, refusal = _resolve_target(operation, environment, rel)
    if refusal is not None:
        return refusal

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _failed(
            operation,
            f"no such file: {rel} (fs.edit only edits existing files; " "use fs.write to create)",
        )
    except UnicodeDecodeError:
        return _failed(
            operation, f"cannot edit {rel}: not valid UTF-8 text (fs.edit works on text files)"
        )
    except OSError as exc:
        return _failed(operation, f"cannot read {rel}: {exc}")

    if old == "":
        return _failed(operation, "old_string must be non-empty; use fs.write to create a file")
    if old == new:
        return _failed(operation, "old_string and new_string are identical (no-op edit)")

    count = text.count(old)
    if count == 0:
        return _failed(
            operation,
            f"old_string not found in {rel} (it must match the file exactly, "
            "including whitespace and indentation)",
        )
    if count > 1 and not replace_all:
        return _failed(
            operation,
            f"old_string is not unique in {rel} ({count} matches); add surrounding "
            "context to disambiguate, or set replace_all=true",
        )

    replacements = count if replace_all else 1
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

    try:
        path.write_text(updated, encoding="utf-8", newline="")
    except OSError as exc:
        return _failed(operation, f"cannot write {rel}: {exc}")

    # Only the bytes this edit authored into the file (replacement text times
    # occurrences replaced) — never the whole file (tools.py:938-941).
    n_bytes = replacements * len(new.encode("utf-8"))
    plural = "occurrence" if replacements == 1 else "occurrences"
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.SUCCEEDED,
        output={"path": rel, "bytes_written": n_bytes, "replacements": replacements},
        rendering=f"edited {rel}: replaced {replacements} {plural}",
        effects=Effects(changed_paths=(rel,), bytes_written=n_bytes, complete=True),
    )


# Self-registering on import — no shared registry file needs editing. Both
# kinds declare intent=MUTATE (they preview by default) and reuse the OBSERVE
# profile: ExecutionProfile categorizes *subprocess* trust ("why a subprocess
# is running"), and neither handler spawns one. Of the three declared values,
# OBSERVE is the only one that does not itself claim an elevated-trust
# subprocess category, and its own docstring already generalises to "confined
# to the selected root; never implies process isolation" — the property that
# actually applies here.
register(
    WRITE_KIND,
    intent=OperationIntent.MUTATE,
    default_profile=ExecutionProfile.OBSERVE,
    run=_write,
)
register(
    EDIT_KIND,
    intent=OperationIntent.MUTATE,
    default_profile=ExecutionProfile.OBSERVE,
    run=_edit,
)
