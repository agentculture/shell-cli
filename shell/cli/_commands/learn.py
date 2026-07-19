"""``shell learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from shell import __version__
from shell.cli._output import emit_result

_TEXT = """\
shell — the file-and-shell tool surface for AI coding agents.

Purpose
-------
Read, write, edit, list, view media, and gated shell execution — with path
confinement and an operator approval policy. Packaged pure-stdlib so any agent
harness imports one safe execution layer instead of reimplementing it, and
reimplementing its safety model with it. Extracted from `colleague`, which is
the first consumer rather than the owner.

Safety posture
--------------
A guard, NOT a sandbox. The execution gate is best-effort and is bypassable by
`sh -c`, pipelines, and shell expansion. It protects against accidental and
careless behaviour, not an adversarial one. Read `shell explain safety` before
relying on it.

Status
------
The library has the operation core: fs.read/list/write/edit/media, the policy
evaluator, the evidence contract, and HostRunner execution are built and green.
The CLI is catching up: `policy` and `operation` are now real CLI nouns --
`policy check`/`policy explain` evaluate the same policy gate execute() uses,
`operation show` retrieves persisted evidence. The env/fs/process/git verb
groups are not exposed yet -- no CLI verb executes fs.read or a shell command
itself, only ones that introspect the policy/evidence around that execution.
See https://github.com/agentculture/shell-cli/issues/1

Commands
--------
  shell whoami             Identity from culture.yaml.
  shell learn              This self-teaching prompt.
  shell explain <path>...  Markdown docs for any noun/verb path.
  shell overview           Descriptive snapshot of the agent.
  shell doctor             Check the agent-identity invariants.
  shell cli overview       Describe the CLI surface itself.
  shell policy check       Evaluate the run_command policy gate.
  shell policy explain     List gated/ungated operation kinds.
  shell operation show     Retrieve persisted operation evidence.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  shell explain shell
  shell explain safety
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "shell",
        "distribution": "shell-cli",
        "version": __version__,
        "purpose": (
            "The file-and-shell tool surface for AI coding agents: read, write, edit, "
            "list, view media, and gated shell execution, with path confinement and an "
            "operator approval policy. Pure-stdlib core."
        ),
        "safety_posture": (
            "A guard, not a sandbox. The execution gate is best-effort and bypassable "
            "by `sh -c`, pipelines, and shell expansion; it protects against accidental "
            "and careless behaviour, not an adversarial one."
        ),
        "status": (
            "the operation core is built in the library (fs primitives, policy, "
            "evidence, HostRunner); the CLI now also exposes policy and operation "
            "as real nouns -- env/fs/process/git verb groups are still library-only"
        ),
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {
                "path": ["policy", "check"],
                "summary": "Evaluate the run_command policy gate for one operation.",
            },
            {
                "path": ["policy", "explain"],
                "summary": "List gated kind prefixes and every kind's policy status.",
            },
            {
                "path": ["operation", "show"],
                "summary": "Retrieve persisted evidence for an operation id.",
            },
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "shell explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
