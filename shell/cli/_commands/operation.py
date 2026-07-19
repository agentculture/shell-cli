"""``shell operation`` — retrieve persisted evidence for a previously executed operation.

One verb: ``operation show <operation-id>``. It does not execute anything and
does not construct an :class:`~shell.operations.Operation` — it reads a
persisted :class:`~shell.evidence.EvidenceRecord` off disk from an
:class:`~shell.evidence.EvidenceStore` directory, exactly as
``docs/evidence-contract.md`` describes it.

**Persistence is opt-in.** ``shell.operations.execute()`` only writes a record
when its caller passes ``evidence_store=...``; most invocations configure
none. That means "no record found here" is the ordinary case, not a bug, and
this module is careful to say so rather than let a missing directory read as
"nothing ever happened" — the two are different claims and only one of them is
true. Three outcomes are kept distinct, matching the honest-degradation
posture the rest of this package uses:

* **no evidence directory at this location** — most likely no store was ever
  configured to write here, or the caller used a different ``--evidence-dir``.
  Reported as a user error (exit 1): the lookup did not resolve, the same
  shape as ``shell explain`` on an unknown path.
* **a directory exists, but no record in it matches the given operation id** —
  reported the same way, with the count of records that *were* found so the
  caller can tell "wrong id" from "empty store" at a glance.
* **the location exists but cannot actually be read** (not a directory,
  permission denied, or another OS-level failure) — reported as an
  environment error (exit 2). This is the one path in this verb surface
  where :data:`shell.cli._errors.EXIT_ENV_ERROR` is genuinely reachable: the
  problem is with the environment (a broken or inaccessible store path), not
  with what the caller typed.

**A finding surfaced while building this verb, not fixed here because
``shell/evidence.py`` is not this slice's to change:** the ``persistence``
block ``docs/evidence-contract.md`` documents as a top-level part of "the
record" is only ever added to the **in-memory** :class:`~shell.evidence.EvidenceRecord`
:func:`shell.evidence.capture` returns to a direct caller or an
``evidence_sink``. :meth:`~shell.evidence.EvidenceStore.write` persists the
*pre-persistence* record to disk before the write outcome exists to describe,
and nothing re-writes the file afterwards. A record this command retrieves
from disk therefore almost never carries a ``persistence`` key at all. See
:func:`_persistence_line` for how that gap is reported rather than papered
over.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from shell.cli._commands.overview import emit_overview
from shell.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from shell.cli._output import emit_result
from shell.evidence import DEFAULT_STORE_SUBDIR, EvidenceStore

_NOUN_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "operation show <operation-id> — retrieve persisted evidence for one operation",
            "operation overview — describe the operation noun (this command)",
        ],
    },
    {
        "title": "Notes",
        "items": [
            "evidence persistence is opt-in (docs/evidence-contract.md); most "
            "operations have no evidence store configured and leave no trail",
            "default lookup directory: ./.shell/evidence (same layout "
            "EvidenceStore.for_environment anchors under an Environment's "
            "source_root)",
            "a missing directory or an unmatched id is a user error (exit 1); "
            "a store that exists but cannot be read is an environment error (exit 2)",
        ],
    },
]


def _default_evidence_dir() -> Path:
    return Path.cwd() / DEFAULT_STORE_SUBDIR


def _probe_directory(directory: Path) -> bool:
    """Return whether *directory* exists and is genuinely readable.

    ``False`` for "does not exist" — that is the ordinary opt-in-persistence
    case, not a failure, and the caller decides how to report it. A path that
    exists but is not a directory, or exists and cannot be listed, is a
    distinct and worse fact: something is actually broken here, so it raises
    rather than degrading to "not found" the way :class:`~shell.evidence.EvidenceStore`
    itself does internally (that module is written to never abort a running
    pipeline; this CLI command has no pipeline to protect and can afford to
    say so).
    """
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"evidence path exists but is not a directory: {directory}",
            remediation="point --evidence-dir at a directory (see docs/evidence-contract.md)",
        )
    try:
        list(directory.iterdir())
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"evidence directory could not be read: {directory} "
                f"({type(exc).__name__}: {exc})"
            ),
            remediation="check permissions on the evidence directory",
        ) from exc
    return True


def _find_record(directory: Path, operation_id: str) -> dict[str, Any]:
    if not _probe_directory(directory):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no evidence directory at {directory}",
            remediation=(
                "evidence persistence is opt-in (docs/evidence-contract.md); no "
                "trail exists here unless a caller configured an EvidenceStore at "
                "this exact location for that operation -- pass --evidence-dir to "
                "point at the right location"
            ),
        )

    store = EvidenceStore(directory=directory)
    records = store.records()
    matches = [r for r in records if r.get("operation_id") == operation_id]
    if not matches:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"no evidence record for operation {operation_id!r} in {directory} "
                f"({len(records)} record(s) present)"
            ),
            remediation="check the operation id and --evidence-dir",
        )
    # Records are unique per operation id in normal use (ids default to a
    # fresh uuid4 per Operation); if a caller reused an id, the most recently
    # written record wins, matching EvidenceStore.records()'s chronological
    # (oldest-first) ordering.
    return matches[-1]


def _persistence_line(record: dict[str, Any]) -> str:
    """Render the ``persistence`` block, honestly, when the on-disk body lacks one.

    A record found by this command was, by construction, read from a file that
    exists — so it was written. That is not the same claim as the record's own
    ``persistence.persisted`` field, and the two currently diverge: ``EvidenceStore.write``
    (``shell/evidence.py``) persists ``record`` to disk *before* the write
    outcome is known, so the ``persistence`` block ``capture()`` builds
    afterwards — the one ``docs/evidence-contract.md`` documents as a top-level
    block of "the record" — is added only to the in-memory copy handed to a
    direct caller or ``evidence_sink``, never re-written back to the file this
    command reads. So a record retrieved here almost always has no
    ``persistence`` key at all, and that must not be misreported as
    ``persisted: None`` (which reads as "unknown or false" to a caller
    skimming for a boolean). Displaying the honest reason is safer than
    filling the gap with a value this module cannot actually verify.
    """
    persistence = record.get("persistence")
    if persistence is not None:
        return f"  persisted: {persistence.get('persisted')} path={persistence.get('path')}"
    return (
        "  persisted: (this record's stored body has no persistence block -- "
        "EvidenceStore.write() persists a record before its own write outcome "
        "is known, so a successful write never carries this field back to disk; "
        "being retrievable here is itself the evidence that it was written)"
    )


def _render_text(record: dict[str, Any]) -> str:
    execution = record.get("execution", {})
    policy = record.get("policy", {})
    effects = record.get("effects", {})
    quality = record.get("evidence_quality", {})
    lines = [
        f"operation {record.get('operation_id')}",
        f"  status: {record.get('status')}",
        f"  kind: {record.get('operation', {}).get('kind')}",
        f"  policy: {policy.get('decision')} — {policy.get('reason')}",
        f"  applied: {execution.get('applied')} "
        f"(handler_disposition={execution.get('handler_disposition')})",
        (
            f"  effects: complete={effects.get('complete')} "
            f"changed_paths={len(effects.get('changed_paths', []))} "
            f"bytes_written={effects.get('bytes_written')}"
        ),
        f"  evidence_degraded: {quality.get('degraded')}",
        _persistence_line(record),
    ]
    return "\n".join(lines)


def cmd_operation_show(args: argparse.Namespace) -> int:
    directory = args.evidence_dir if args.evidence_dir is not None else _default_evidence_dir()
    record = _find_record(directory, args.operation_id)
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(record, json_mode=True)
    else:
        emit_result(_render_text(record), json_mode=False)
    return 0


def cmd_operation_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "shell operation",
        _NOUN_SECTIONS,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_operation_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "operation",
        help="Inspect persisted operation evidence (see 'shell operation overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="operation_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the operation noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_operation_overview)

    show = noun_sub.add_parser(
        "show",
        help="Retrieve the persisted evidence record for an operation id.",
    )
    show.add_argument("operation_id", help="The operation id to look up.")
    show.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Evidence store directory to search (default: ./.shell/evidence).",
    )
    show.add_argument("--json", action="store_true", help="Emit structured JSON.")
    show.set_defaults(func=cmd_operation_show)
