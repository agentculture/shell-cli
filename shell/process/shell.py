"""``process.shell``: run a model-authored command string through a fresh shell.

This is the kind the first consumer's ``run_command`` maps onto, and it is the
one place in this package where a string that something else will re-interpret
is executed on purpose. Everything about how it is described exists to keep that
fact in front of whoever reads a result.

What is not true of this operation
----------------------------------

The command is **not path-confined**. Its cwd is the environment's work root,
but a cwd is a starting directory and not a boundary: the command can leave the
work root through shell expansion, interpreters, absolute paths, network calls,
or child processes. Structured filesystem operations (``fs.read``, ``fs.write``)
resolve their target against the work root and refuse to leave it; this
operation has no comparable check and no comparable guarantee, and the two must
never be described in one sentence.

The policy gate is **not containment** either. It reads the first token of the
string, and the shell re-tokenizes the whole string afterwards — so ``sh -c``,
pipelines, here-docs, command substitution, variable expansion and an absolute
path to a renamed binary all step around it. It encodes operator intent against
accidental and careless behaviour, never against an adversarial one.

None of that lives only in this docstring. Every result carries
``output["confinement"]`` stating ``path_confined: False`` alongside
:data:`SHELL_CONFINEMENT_NOTE`, and the pipeline stamps the runner's own
``isolation``/``isolation_note`` onto every result's evidence. A consumer that
never reads a document still receives the posture in the payload.

Project profile only
--------------------

``process.shell`` accepts the ``project`` profile and refuses every other one.
That is not a style rule. A ``control`` operation runs a trusted control-plane
program — git mechanics, a capability CLI, the container runtime — and which
program runs must be settled by the argv vector the caller assembled, not by a
string a shell gets to reinterpret after the gate has read it. A caller wanting
a control-profile process uses ``process.exec``; refusing here is what stops a
raw string from acquiring control-plane trust by asking for it.

The practical consequence is that ``process.shell`` and a control-profile hook
or CLI invocation land in evidence with *different* profiles, from different
kinds, and an auditor separating model-authored execution from control-plane
execution can do it on those two fields alone.

Parity with ``run_command``
---------------------------

Preserved from ``colleague/tools.py:957`` (pinned SHA ``28fee29``): a **fresh
shell** per invocation (no state survives between commands), **cwd rooted** at
the work root, and a **bounded timeout** resolved from the operation or the
environment. Output is where the neutral shape and the compat shape diverge on
purpose — the first consumer concatenates stdout and stderr into one unlabelled
string (``tools.py:1047-1048``) while this package captures them separately.
Concatenating is lossy and cannot be undone, so the neutral result keeps them
apart and the adapter composes the legacy string on its side.
``tests/test_process_shell.py`` pins that the composition reproduces colleague's
committed fixtures byte-for-byte, which is what makes "the adapter can do it"
a checked claim rather than an assurance.
"""

from __future__ import annotations

from pathlib import Path

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.process.exec import (
    build_env,
    confinement_block,
    execute_request,
    failed,
    require_profile,
)
from shell.results import OperationResult
from shell.runners.types import ProcessRequest

__all__ = ["KIND", "SHELL_CONFINEMENT_NOTE"]

#: The operation kind this module registers.
KIND = "process.shell"

#: Carried on every ``process.shell`` result's ``output`` payload. The single
#: canonical statement of what this operation does not do, kept as a constant so
#: the result metadata and the prose quote one string rather than two drifting
#: paraphrases.
SHELL_CONFINEMENT_NOTE = (
    "A raw shell command is not path-confined. Its cwd is the work root, but a "
    "cwd is a starting directory and not a boundary: the command can leave the "
    "work root through shell expansion, interpreters, absolute paths, network "
    "calls, or child processes. The policy gate read the first token of a "
    "string that the shell then re-tokenizes, so 'sh -c', pipelines, here-docs, "
    "command substitution and variable expansion all step around it. This "
    "guards against accidental and careless behaviour, never an adversarial "
    "one, and confinement of a process belongs to the runner axis rather than "
    "to this operation."
)

_SHELL_PROFILES = (ExecutionProfile.PROJECT,)


def _shell(operation: Operation, environment: Environment) -> OperationResult:
    arguments = operation.arguments

    if "argv" in arguments:
        return failed(
            operation,
            f"{KIND} takes a shell command string, not an argv vector. Pass "
            "'command' as a string, or use process.exec for an argv vector "
            "(which is what a control-profile operation must use).",
        )

    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return failed(operation, f"{KIND} requires 'command': a non-empty shell command string")

    problem = require_profile(
        operation,
        KIND,
        _SHELL_PROFILES,
        "A raw shell string may not carry control-plane trust: the shell "
        "re-interprets it after the gate read it, so which program runs is not "
        "settled by what was gated. Use process.exec with an argv vector.",
    )
    if problem is not None:
        return failed(operation, problem)

    request = ProcessRequest(
        command=command,
        cwd=Path(environment.work_root),
        env=build_env(environment),
        timeout_seconds=operation.resolved_timeout(environment),
        max_output_bytes=operation.resolved_max_output_bytes(environment),
    )
    return execute_request(
        operation,
        environment,
        request,
        kind=KIND,
        confinement=confinement_block(note=SHELL_CONFINEMENT_NOTE, uses_shell=True),
        details={"command": command},
    )


# Self-registering on import, like every other handler. ``intent=EXECUTE`` means
# this previews unless the caller passes ``apply=True``, and a preview describes
# what *would* run without predicting what it would change.
register(
    KIND,
    intent=OperationIntent.EXECUTE,
    default_profile=ExecutionProfile.PROJECT,
    run=_shell,
)
