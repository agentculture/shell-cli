"""``process.exec``: run an explicit argv vector, and the machinery both process
operations share.

An argv vector is handed to the runner as a list of strings. Nothing splits it,
expands it, substitutes into it, or re-reads it — the program and its arguments
are exactly the tuple the caller assembled. That is the whole reason a *control*
operation may use this kind and may never use :mod:`shell.process.shell`: a raw
string is re-interpreted by a shell after the gate has already read it, and a
trusted control-plane program must not be selected by a string that something
else gets to reinterpret.

What that does and does not buy
-------------------------------

Removing shell re-interpretation removes one specific weakness and nothing else.
The process still runs on whatever the environment's runner is; under
:class:`~shell.runners.host.HostRunner` that is the operator's own machine, with
the operator's reach. An argv vector naming ``/bin/sh`` with ``-c`` and a string
is back to square one, and this module does not pretend otherwise — it declares
``path_confined: False`` on every result, exactly as ``process.shell`` does.
The difference between the two kinds is recorded as ``uses_shell`` and
``gate_inspects_reinterpreted_string``, which are separate facts from
confinement, and they are kept separate deliberately.

Profiles
--------

The profile answers "why is this subprocess running", and it is recorded on the
normalized operation in every evidence record. ``process.exec`` accepts
``project`` (repository-controlled code: tests, linters, project hooks) and
``control`` (trusted control-plane programs: git mechanics, capability CLIs).
It refuses ``observe``, which is the profile for structured reads that spawn
nothing at all.

The registered default is ``project`` — the *least* trusted of the two it
accepts. Elevated trust has to be asked for explicitly, because a default that
hands control-plane trust to an unlabelled caller is the kind of implicit
decision that later reads as a deliberate one.

A non-zero exit is a SUCCEEDED operation
----------------------------------------

The operation is "run this process and observe it". A test suite that exits 3
ran fine; its exit code is the *result being reported*, not a failure of the
operation to happen. So ``exit_code`` carries the verdict and
:attr:`~shell.results.OperationStatus.SUCCEEDED` means the process ran to
completion and was observed. This matches the first consumer, whose
``run_command`` returns a successful tool call for a non-zero exit
(``tests/fixtures/colleague/behavior.json``,
``run_command_exit_code_and_body_shape`` is ``ok: true`` for ``exit=3``).

``FAILED`` is therefore reserved for the process not having run as asked: it
could not be started, it was cancelled, or it never reported an exit code.
``TIMED_OUT`` is its own peer status, because a command that was cut off never
reported anything about its work.

Effects are never complete here
-------------------------------

Both process kinds report ``Effects(complete=False)`` with an empty changed-path
list, always. A host process may write anywhere it can reach, and no inspection
at this layer will enumerate that. The empty list is "nothing was observed", not
"nothing happened" — ``tests/test_process_shell.py`` pins that distinction with a
real command that writes a file it never declared.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.results import Effects, Evidence, OperationResult, OperationStatus
from shell.runners.types import ProcessOutcome, ProcessRequest

__all__ = ["EXEC_CONFINEMENT_NOTE", "KIND"]

#: The operation kind this module registers.
KIND = "process.exec"

#: Stated once, carried on every ``process.exec`` result's ``output`` payload.
#: A consumer that never reads a document still receives it.
EXEC_CONFINEMENT_NOTE = (
    "An argv vector is handed to the runner as-is: no shell splits, expands or "
    "substitutes into it, so nothing in it is re-interpreted after the policy "
    "gate read it. That removes one weakness and nothing more. The process is "
    "not path-confined: it runs on the selected runner with that runner's "
    "reach, and can read or write outside the work root through absolute paths, "
    "interpreters, network calls, or child processes. The cwd is a starting "
    "directory, never a boundary. Confinement of a process belongs to the "
    "runner axis, and the host runner declares none."
)

_EXEC_PROFILES = (ExecutionProfile.PROJECT, ExecutionProfile.CONTROL)


def truncate(text: str, limit: int) -> str:
    """Bound *text* to *limit* characters with a visible marker.

    Same shape as the first consumer's ``_truncate`` (``colleague/tools.py:724``)
    so a bounded rendering reads identically on both sides of the seam during
    parity.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated at {limit} chars]"


def failed(operation: Operation, message: str) -> OperationResult:
    """A known, recoverable argument or profile error.

    Returned rather than raised: the caller is an agent loop, and a malformed
    argument must cost one model-visible step, never the run. Effects are
    complete here because nothing was started — this is the one place in this
    module where an empty effect list is an earned claim rather than an
    unobserved one.
    """
    return OperationResult(
        operation_id=operation.id,
        status=OperationStatus.FAILED,
        error=message,
        rendering=message,
        effects=Effects(complete=True),
    )


def build_env(environment: Environment) -> dict[str, str]:
    """Reduce the host environment to the names *environment* allow-lists.

    An allow-list, never a deny-list, and never a passthrough with exceptions:
    an empty :attr:`~shell.environment.Environment.env_passthrough` yields a
    genuinely empty environment. Nothing here quietly restores ``PATH`` or
    ``HOME``, because a runner or handler adding a variable back would make the
    caller's declared policy a fiction — the same reasoning
    :class:`~shell.runners.types.ProcessRequest` states for its own three-valued
    ``env``.

    Values are read but never recorded; only the *names* reach evidence.
    """
    source = os.environ
    return {name: source[name] for name in environment.env_passthrough if name in source}


def require_profile(
    operation: Operation,
    kind: str,
    allowed: tuple[ExecutionProfile, ...],
    hint: str,
) -> str | None:
    """Return an error message when *operation* declares a profile *kind* refuses.

    ``None`` means the profile is acceptable. The profile is always resolved by
    :func:`shell.operations.normalize` before a handler runs, so this never sees
    ``None`` on the operation.
    """
    profile = operation.profile
    if profile in allowed:
        return None
    names = ", ".join(repr(p.value) for p in allowed)
    declared = "unset" if profile is None else repr(profile.value)
    return f"{kind} may not run under the {declared} profile (accepts {names}). {hint}"


def confinement_block(*, note: str, uses_shell: bool) -> dict[str, Any]:
    """The non-confinement facts a consumer reads off the result itself.

    ``path_confined`` is ``False`` for both process kinds and is not a field a
    handler may set to anything else. ``uses_shell`` and
    ``gate_inspects_reinterpreted_string`` record the *additional* weakness a
    raw string carries; they are separate keys from ``path_confined`` because
    they are separate facts, and folding them together would let a reader infer
    that an argv vector is confined merely because it is not re-interpreted.
    """
    return {
        "path_confined": False,
        "uses_shell": uses_shell,
        "gate_inspects_reinterpreted_string": uses_shell,
        "note": note,
    }


def _status(kind: str, outcome: ProcessOutcome) -> tuple[OperationStatus, str]:
    """Map a neutral process outcome onto an operation status and error text.

    See the module docstring: a non-zero exit is a success of the *operation*.
    """
    if outcome.exit_code is None and outcome.error:
        return OperationStatus.FAILED, f"{kind} could not start the process: {outcome.error}"
    if outcome.timed_out:
        return (
            OperationStatus.TIMED_OUT,
            f"{kind} exceeded its timeout and was terminated after "
            f"{outcome.duration_ms / 1000.0:.1f}s",
        )
    if outcome.cancelled:
        return OperationStatus.FAILED, f"{kind} was cancelled and terminated"
    if outcome.exit_code is None:
        return (
            OperationStatus.FAILED,
            f"{kind} reported no exit code: the process was not reaped after termination",
        )
    return OperationStatus.SUCCEEDED, ""


def render(kind: str, outcome: ProcessOutcome, limit: int) -> str:
    """A bounded, labelled rendering that keeps the two streams distinguishable.

    This is NOT the first consumer's legacy ``f"exit={code}\\n{stdout}{stderr}"``
    string, and deliberately so: that concatenation throws away which stream a
    line came from, and a neutral record that has thrown the distinction away
    cannot get it back. The adapter composes the legacy form from
    ``evidence.exit_code``/``evidence.stdout``/``evidence.stderr``, which is
    pinned byte-for-byte against the committed colleague fixtures in
    ``tests/test_process_shell.py``.
    """
    head = f"{kind} exit={outcome.exit_code}"
    if outcome.timed_out:
        head += " (timed out)"
    if outcome.cancelled:
        head += " (cancelled)"
    lines = [head]
    if outcome.error:
        lines.append(f"error: {outcome.error}")
    if outcome.stdout:
        lines.append(f"--- stdout ---\n{outcome.stdout}")
    if outcome.stderr:
        lines.append(f"--- stderr ---\n{outcome.stderr}")
    if outcome.stdout_truncated or outcome.stderr_truncated:
        lines.append(f"[output bounded at {limit} bytes per stream]")
    if not outcome.output_complete:
        lines.append(
            "[captured output is a prefix: the readers were still blocked when "
            "the runner stopped waiting for them]"
        )
    return truncate("\n".join(lines), limit)


def _evidence(kind: str, outcome: ProcessOutcome) -> Evidence:
    """Handler-owned evidence: exit code, both streams, and honest gaps.

    ``stdout`` and ``stderr`` go into separate fields and stay there. Everything
    about *where* this ran is stamped by :func:`shell.operations.execute`, not
    claimed here.

    A capture that stopped short marks the evidence ``degraded``. An incomplete
    stream is a real gap in the record, and reporting it only inside ``output``
    would leave the standard "was this record any good?" field saying yes.
    """
    evidence = Evidence(
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        stdout_truncated=outcome.stdout_truncated,
        stderr_truncated=outcome.stderr_truncated,
        stdout_bytes=outcome.stdout_bytes,
        stderr_bytes=outcome.stderr_bytes,
        cwd=outcome.cwd,
    )
    if outcome.output_complete:
        return evidence
    return replace(
        evidence,
        degraded=True,
        degraded_reason=(
            f"{kind}: the captured streams are a prefix. The output readers were "
            "still blocked when the runner stopped waiting, which happens when a "
            f"descendant outlives the command and holds the pipe open. "
            f"{outcome.termination.note}"
        ),
    )


def execute_request(
    operation: Operation,
    environment: Environment,
    request: ProcessRequest,
    *,
    kind: str,
    confinement: Mapping[str, Any],
    details: Mapping[str, Any],
) -> OperationResult:
    """Run *request* on the environment's runner and shape the neutral result.

    Shared by both process kinds so there is exactly one place where a process
    outcome becomes an operation result. The two handlers differ only in what
    they validate, what they put in *details*, and which confinement note they
    carry — never in how an outcome is interpreted.

    The runner is asked for the process; no handler spawns one itself. That is
    what lets a container runner satisfy the same call later without either
    handler learning anything about containers.
    """
    outcome = environment.runner.run_process(request)
    limit = operation.resolved_max_output_bytes(environment)
    status, error = _status(kind, outcome)

    output: dict[str, Any] = {
        **details,
        # The profile is on the normalized operation in every evidence record;
        # it is echoed here so a consumer reading only the result still sees why
        # this subprocess was trusted to run.
        "profile": None if operation.profile is None else operation.profile.value,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "cancelled": outcome.cancelled,
        "process_duration_ms": outcome.duration_ms,
        "cwd": outcome.cwd,
        "timeout_seconds": request.timeout_seconds,
        "max_output_bytes": request.max_output_bytes,
        # Names only. A value never leaves this process by way of a result.
        "env_names": sorted(request.env or {}),
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_bytes": outcome.stderr_bytes,
        "output_complete": outcome.output_complete,
        "termination": outcome.termination.to_dict(),
        "confinement": dict(confinement),
    }

    return OperationResult(
        operation_id=operation.id,
        status=status,
        output=output,
        rendering=render(kind, outcome, limit),
        error=error,
        # Never complete. A process may write anywhere it can reach; an empty
        # changed-path list here means "not observed", not "nothing happened".
        effects=Effects(complete=False),
        evidence=_evidence(kind, outcome),
    )


def _exec(operation: Operation, environment: Environment) -> OperationResult:
    arguments = operation.arguments

    if "command" in arguments:
        return failed(
            operation,
            f"{KIND} takes an argv vector, never a raw shell string. Pass "
            "'argv' as a list of strings, or use process.shell for a shell "
            "string (which is a project-profile operation only).",
        )

    argv = arguments.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        return failed(operation, f"{KIND} requires 'argv': a non-empty list of strings")

    problem = require_profile(
        operation,
        KIND,
        _EXEC_PROFILES,
        "The 'observe' profile is for structured reads that spawn no process.",
    )
    if problem is not None:
        return failed(operation, problem)

    request = ProcessRequest(
        argv=tuple(str(part) for part in argv),
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
        confinement=confinement_block(note=EXEC_CONFINEMENT_NOTE, uses_shell=False),
        details={"argv": list(request.argv)},
    )


# Self-registering on import. ``intent=EXECUTE`` is what makes this preview
# unless the caller passes ``apply=True``; ``default_profile=PROJECT`` is the
# less-trusted of the two profiles this kind accepts, so control-plane trust is
# always something a caller asked for in writing.
register(
    KIND,
    intent=OperationIntent.EXECUTE,
    default_profile=ExecutionProfile.PROJECT,
    run=_exec,
)
