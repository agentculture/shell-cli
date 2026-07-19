"""``shell policy`` — evaluate and explain the operation-policy gate.

Two verbs, both reads:

* ``policy check`` answers *would this operation be gated, and how* — for one
  ``(kind, arguments)`` pair.
* ``policy explain`` answers *what does this policy gate at all* — the gated
  kind prefixes, and the explicit ungated/allowed/denied status of every
  operation kind this build currently knows about.

**Both call :func:`shell.operations._policy_gate` directly — the exact
function :func:`shell.operations.execute` invokes on its own gate path.** This
is deliberate, not an implementation shortcut: acceptance for this verb
surface requires the CLI to report the *same* :class:`~shell.results.PolicyVerdict`
the execution path would apply, not a hand-rolled re-derivation of its rules
that could silently drift from the real gate (jurisdiction by
``GATED_KIND_PREFIXES``, the untrustworthy-policy-fails-closed branch, the
trust-note annotation). Importing a name prefixed ``_`` across a module
boundary is unusual; here it is the whole point; ``tests/test_dispatch_policy.py``
and ``tests/test_operations.py`` already reach for the same private name as a
monkeypatch target, so this is an established pattern in this package, not a
new one.

One caveat worth stating plainly: :func:`shell.operations.execute` gates the
*post-rewrite* operation, and a rewrite is supplied per-call by whoever calls
``execute`` (colleague's ``pre_tool`` hook, for instance). This CLI has no
rewrite to apply — it evaluates the ``(kind, arguments)`` pair exactly as
given, which is what the gate would see for that pair absent any rewrite.

Neither verb requires the checked operation kind to be a *registered* handler.
``_policy_gate`` only reads ``operation.kind`` and ``operation.arguments``; it
never looks the kind up in the handler registry. This is what lets
``policy check --kind process.shell ...`` answer honestly even before the
``process.*`` handlers exist (Milestone 1 t84, not yet merged into this
worktree) — the gate's jurisdiction is a property of the kind *string*, not of
whether a handler happens to be registered for it.
"""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Any

from shell import operations
from shell.cli._commands.overview import emit_overview
from shell.cli._errors import EXIT_USER_ERROR, CliError
from shell.cli._output import emit_result
from shell.operations import Operation
from shell.policy import FILE_CATEGORIES, Policy, load_policy

# Importing these registers fs.read / fs.list / fs.write / fs.edit / fs.media
# as a side effect (see shell/fs/__init__.py — the package marker is
# deliberately empty; consumers import each handler module explicitly). This
# guarantees `policy explain` reports the real, currently-built kind set
# regardless of what else this pytest worker happened to import first — the
# exact hazard the task's parallel-test-worker warning names. Registration is
# idempotent per interpreter (Python caches the module import), so importing
# it here and again in a test module is never a double-registration error.
import shell.fs.list  # noqa: E402,F401  isort:skip
import shell.fs.media  # noqa: E402,F401  isort:skip
import shell.fs.read  # noqa: E402,F401  isort:skip
import shell.fs.write  # noqa: E402,F401  isort:skip

_NOUN_SECTIONS = [
    {
        "title": "Verbs",
        "items": [
            "policy check <kind> — evaluate the run_command gate for one operation",
            "policy explain — list gated kind prefixes and every kind's status",
            "policy overview — describe the policy noun (this command)",
        ],
    },
    {
        "title": "Notes",
        "items": [
            "both verbs call shell.operations._policy_gate directly — the same "
            "function the execution path gates through, not a re-derivation",
            "fs.* is deliberately NOT policy-gated; run_command policy has "
            "jurisdiction only over process.* kinds (GATED_KIND_PREFIXES)",
            "an ungated verdict is not the same as an allowed one — see "
            "shell.results.PolicyDecision",
        ],
    },
]


def _load_policy_from_args(args: argparse.Namespace) -> Policy:
    """Build the :class:`Policy` snapshot ``check``/``explain`` evaluate against.

    Mirrors :func:`shell.policy.load_policy`'s own contract: no candidate files
    means the empty policy, which is exactly what ``execute()`` substitutes
    when a caller passes no policy at all. A malformed ``--policy-json`` value
    is the one case this module raises on — the CLI is the authoring side here
    (a human typed the flag), and :func:`shell.policy.load_policy` itself never
    raises for a bad *file*, only for a bad inline ``data`` mapping shape it
    cannot recover from silently.
    """
    candidates: list[Path] = list(getattr(args, "policy_file", None) or [])
    data: dict[str, Any] | None = None
    raw = getattr(args, "policy_json", None)
    if raw:
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"--policy-json is not valid JSON: {exc}",
                remediation='pass a JSON object, e.g. \'{"run_command": {"deny": ["rm"]}}\'',
            ) from exc
        if not isinstance(parsed, dict):
            raise CliError(
                code=EXIT_USER_ERROR,
                message="--policy-json must be a JSON object",
                remediation="pass an object, e.g. '{\"run_command\": {...}}'",
            )
        data = parsed
    return load_policy(candidates, data=data)


def _build_operation(args: argparse.Namespace) -> Operation:
    arguments: dict[str, Any] = {}
    command = getattr(args, "raw_command", None)
    argv = getattr(args, "argv", None)
    if command:
        arguments["command"] = command
    elif argv:
        arguments["argv"] = list(argv)
    return Operation(kind=args.kind, arguments=arguments)


# --- check ------------------------------------------------------------------


def cmd_policy_check(args: argparse.Namespace) -> int:
    policy = _load_policy_from_args(args)
    operation = _build_operation(args)

    # THE call: shell.operations.execute() gates through this exact function.
    verdict = operations._policy_gate(operation, policy)

    payload = {
        "kind": operation.kind,
        "arguments": dict(operation.arguments),
        "gated_by_prefix": operation.kind.startswith(operations.GATED_KIND_PREFIXES),
        "verdict": verdict.to_dict(),
    }
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        lines = [
            f"kind: {operation.kind}",
            f"decision: {verdict.decision.value}",
            f"reason: {verdict.reason}",
        ]
        if verdict.matched_rule:
            lines.append(f"matched_rule: {verdict.matched_rule}")
        emit_result("\n".join(lines), json_mode=False)
    # A denied verdict is information a read reported successfully, not a CLI
    # failure — this command evaluates a policy, it does not enforce one.
    # Parse the `decision` field rather than the exit code.
    return 0


# --- explain ------------------------------------------------------------------


def _kind_report(kind: str, policy: Policy) -> dict[str, Any]:
    verdict = operations._policy_gate(Operation(kind=kind, arguments={}), policy)
    return {
        "kind": kind,
        "gated_by_prefix": kind.startswith(operations.GATED_KIND_PREFIXES),
        "decision": verdict.decision.value,
        "reason": verdict.reason,
    }


def cmd_policy_explain(args: argparse.Namespace) -> int:
    policy = _load_policy_from_args(args)
    kinds = [_kind_report(kind, policy) for kind in operations.registered_kinds()]

    payload = {
        "gated_kind_prefixes": list(operations.GATED_KIND_PREFIXES),
        "file_categories": sorted(FILE_CATEGORIES),
        "kinds": kinds,
        "policy": policy.to_dict(),
        "note": (
            "a kind not starting with any gated_kind_prefixes entry is reported "
            "'ungated' explicitly, never omitted -- fs.* is deliberately not "
            "policy-gated (confining file operations is the filesystem layer's "
            "job); ungated is distinct from allowed, see PolicyDecision.UNGATED"
        ),
    }
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        lines = [
            f"gated kind prefixes: {', '.join(payload['gated_kind_prefixes']) or '(none)'}",
            f"file categories (check_file): {', '.join(payload['file_categories'])}",
            "",
            "kinds:",
        ]
        for entry in kinds:
            lines.append(
                f"  {entry['kind']}: {entry['decision']}"
                f" (gated_by_prefix={entry['gated_by_prefix']}) — {entry['reason']}"
            )
        if not kinds:
            lines.append("  (no operation kinds registered in this process)")
        emit_result("\n".join(lines), json_mode=False)
    return 0


# --- overview + wiring --------------------------------------------------------


def cmd_policy_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "shell policy",
        _NOUN_SECTIONS,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_policy_overview(args)


def _add_policy_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--policy-file",
        action="append",
        type=Path,
        metavar="PATH",
        help=(
            "A pre-resolved approvals.json candidate (repeatable; later "
            "candidates win per-section). Omit for the empty policy -- the "
            "same default shell.operations.execute() uses when no policy is "
            "supplied."
        ),
    )
    p.add_argument(
        "--policy-json",
        metavar="JSON",
        help='Inline policy document, e.g. \'{"run_command": {"deny": ["rm"]}}\'. '
        "Takes precedence over --policy-file for the sections it defines.",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "policy",
        help="Evaluate and explain the operation-policy gate (see 'shell policy overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser (the top-level subparsers were built with that
    # parser_class); propagate it so every verb under `policy` routes parse
    # errors through the structured error contract instead of argparse's raw
    # stderr/exit 2.
    noun_sub = p.add_subparsers(dest="policy_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the policy noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_policy_overview)

    check = noun_sub.add_parser(
        "check",
        help="Evaluate the run_command policy gate for one (kind, command/argv) pair.",
    )
    check.add_argument("kind", help="Operation kind to evaluate, e.g. process.shell, fs.read.")
    command_group = check.add_mutually_exclusive_group()
    command_group.add_argument(
        "--command",
        # Explicit dest: argparse derives "command" from "--command" by
        # default, which would collide with (and silently clobber, via the
        # subparsers-action namespace merge) the *top-level* subparsers'
        # dest="command" in shell/cli/__init__.py:_build_parser. That
        # collision was caught the hard way — every `policy check` invocation
        # came back with args.command == None regardless of which noun was
        # actually dispatched, because argparse's per-parser default-fill
        # loop stamps this action's own default (None) into the merged
        # namespace after the outer "command"="policy" had already been set.
        dest="raw_command",
        help="A raw shell command string, judged as written.",
    )
    command_group.add_argument(
        "--argv",
        action="append",
        metavar="TOKEN",
        help="One argv token (repeatable, in order): --argv git --argv status.",
    )
    _add_policy_source_args(check)
    check.add_argument("--json", action="store_true", help="Emit structured JSON.")
    check.set_defaults(func=cmd_policy_check)

    explain = noun_sub.add_parser(
        "explain",
        help="List gated kind prefixes and every known operation kind's policy status.",
    )
    _add_policy_source_args(explain)
    explain.add_argument("--json", action="store_true", help="Emit structured JSON.")
    explain.set_defaults(func=cmd_policy_explain)
