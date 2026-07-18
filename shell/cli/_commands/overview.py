"""``shell overview`` — read-only descriptive snapshot of the agent.

Describes the agent to an agent reader: identity (from culture.yaml), the
mission and current build status, the verb surface, and the artifacts this repo
carries. The shared
section/render helpers here are reused by the ``cli`` noun's ``overview`` (see
:mod:`shell.cli._commands.cli`).

Descriptive verbs never hard-fail on a missing target path — an optional
positional ``target`` is accepted and ignored (overview describes this agent,
not an external target), so ``overview <bogus-path>`` still exits 0.
"""

from __future__ import annotations

import argparse

from shell.cli._commands.whoami import report
from shell.cli._output import emit_result

_ARTIFACTS = [
    "culture.yaml + AGENTS.colleague.md — mesh identity (suffix + backend)",
    "docs/threat-model.md — what the guard does and does not protect against",
    ".claude/skills/ — the canonical guildmaster skill kit (cite-don't-import)",
    "docs/skill-sources.md — skill provenance ledger",
    "pyproject.toml + .github/workflows/ — buildable, deployable package baseline",
]

_VERBS = [
    "whoami — identity probe (nick, version, backend, model)",
    "learn — structured self-teaching prompt",
    "explain <path> — markdown docs for a topic",
    "overview — this descriptive snapshot",
    "doctor — check the agent-identity invariants",
]

_MISSION = [
    "owns the file-and-shell tool surface for AI coding agents",
    "read / write / edit / list / view_media / run_command, gated",
    "path confinement + operator approval policy travel with the primitives",
    "pure-stdlib core (zero base dependencies) so any harness can import it",
    "a guard, NOT a sandbox — see `shell explain safety`",
]

_STATUS = [
    "scaffold — only the introspection verbs below are implemented",
    "the six primitives, confinement, and policy are not extracted yet",
    "tracking issue: https://github.com/agentculture/shell-cli/issues/1",
]


def agent_sections() -> list[dict[str, object]]:
    """Sections describing the agent (used by the global verb)."""
    ident = report()
    return [
        {
            "title": "Identity",
            "items": [
                f"nick: {ident['nick']}",
                f"version: {ident['version']}",
                f"backend: {ident['backend']}",
                f"model: {ident['model']}",
            ],
        },
        {"title": "Mission", "items": list(_MISSION)},
        {"title": "Status", "items": list(_STATUS)},
        {"title": "Verbs", "items": list(_VERBS)},
        {"title": "Artifacts", "items": list(_ARTIFACTS)},
    ]


def cli_sections() -> list[dict[str, object]]:
    """Sections describing the CLI surface itself (used by `cli overview`)."""
    return [
        {
            "title": "Verbs",
            "items": list(_VERBS) + ["cli overview — describe the CLI surface (this command)"],
        },
        {
            "title": "Conventions",
            "items": [
                "the executable is `shell`; `shell-cli` is the repo + PyPI distribution",
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "exit codes: 0 success, 1 user error, 2 environment error, 3+ reserved",
                "write verbs are dry-run by default; --apply commits",
            ],
        },
    ]


def render_text(subject: str, sections: list[dict[str, object]]) -> str:
    lines = [f"# {subject}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit_overview(subject: str, sections: list[dict[str, object]], *, json_mode: bool) -> None:
    if json_mode:
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)


def cmd_overview(args: argparse.Namespace) -> int:
    # `target` is accepted for rubric compatibility (descriptive verbs must not
    # hard-fail on a missing path) but overview describes this agent itself.
    emit_overview(
        "shell-cli",
        agent_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Read-only descriptive snapshot of the agent (identity, verbs, artifacts).",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Ignored — overview always describes this agent itself. Accepted so a "
        "stray path argument never hard-fails.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
