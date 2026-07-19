"""The host runner: operations run as ordinary processes on this machine.

**This is a guard, not a sandbox, and the distinction is load-bearing.**

Structured filesystem operations are path-confined: they resolve their target
against the work root and refuse to leave it. A raw host command is not confined
in any comparable sense. It can leave the work root through shell expansion,
interpreters, absolute paths, network calls, or child processes, and the gate
only ever inspects a string that a shell will later re-interpret — so ``sh -c``,
pipelines, here-docs and variable expansion all defeat it.

What this protects against is **accidental and careless** model behaviour, not an
adversarial one. There is no namespace, container, cgroup, or seccomp boundary
here, and nothing in this module should ever be described as if there were.

That posture is not left to documentation: :attr:`HostRunner.isolation` is
``"none"`` and :attr:`HostRunner.isolation_note` states it in prose, and the
lifecycle pipeline copies both onto the evidence of every single result. A
consumer reading a result learns the posture from the payload, not from a README
it may never have read.

Process groups are cleanup, not containment
-------------------------------------------

:meth:`HostRunner.run_process` starts each command in its own process group so
that a timeout or a cancellation can signal *the whole tree* rather than only the
process this runner happens to hold a handle on. Without that, a command like
``sh -c 'sleep 300 & wait'`` leaves the ``sleep`` running after its parent is
reaped, and the caller is told the operation ended.

A process group buys exactly one thing: a kill that reaches descendants. It is a
**cleanup mechanism**. It does not restrict what the command may open, read,
write, or connect to; it does not stop a process from double-forking out of the
group; and a process that detaches itself with ``setsid`` leaves the group and
survives every signal sent here. Do not read "own process group" as a boundary of
any kind.

Per-platform truth about orphan prevention
------------------------------------------

This is uneven across platforms, and saying so is the point — a single sentence
covering "we clean up child processes" would be false on one of them.

* **POSIX (Linux, macOS, BSD).** ``start_new_session=True`` puts the child in a
  new session, so it leads a new process group whose id equals its pid. Signals
  go to the group via :func:`os.killpg` and reach every descendant that has not
  deliberately left it. Orphan prevention here is real but **not total**: a
  descendant that calls ``setsid`` itself, or is re-parented into another group,
  is out of reach.
* **Windows.** POSIX process groups do not exist, ``start_new_session`` is a
  POSIX-only parameter, and :func:`os.killpg` is not present at all. Termination
  falls back to ``TerminateProcess`` on the **direct child only**. Grandchildren
  are *not* terminated and can outlive the operation. This runner does not use
  Job Objects, so nothing here closes that gap. The gap is reported on every
  outcome as ``termination.orphan_prevention == "direct-child-only"``; it is not
  inferred from a platform check the caller has to make itself.

:data:`~shell.runners.types.ORPHAN_PREVENTION` holds the same statement as data,
:meth:`HostRunner.describe` publishes it, and every
:class:`~shell.runners.types.ProcessOutcome` carries it, so a consumer can read
the limit off the payload instead of matching on ``sys.platform``.

Scope
-----

This module owns *how* a process is started, bounded, terminated and observed.
It does not own *which* process runs, which environment variables it inherits, or
what its output means: the ``process.exec`` / ``process.shell`` handlers resolve
those from the :class:`~shell.environment.Environment` and pass an explicit
request down. Keeping that seam clean is what lets a container runner satisfy the
same signature later without either side growing knowledge of the other.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from shell.runners.types import (
    ORPHAN_PREVENTION,
    ProcessOutcome,
    ProcessRequest,
    Termination,
    TerminationReason,
)

__all__ = [
    "HOST_ISOLATION_NOTE",
    "ORPHAN_PREVENTION",
    "HostRunner",
    "ProcessOutcome",
    "ProcessRequest",
    "Termination",
    "TerminationReason",
    "process_group_supported",
]

# The single canonical statement of the host posture. Kept as a constant so the
# CLI, the evidence record and the docs all quote one string rather than three
# drifting paraphrases.
HOST_ISOLATION_NOTE = (
    "Host execution is a guard, not a sandbox. Structured filesystem operations "
    "are confined to the work root, but a process started here can leave it via "
    "interpreters, absolute paths, network calls, or child processes. There is no "
    "namespace, container, or seccomp boundary. It guards against accidental and "
    "careless behaviour, not an adversarial one."
)

#: Default seconds to wait after the polite signal before escalating to the
#: unconditional one. Long enough for a well-behaved program to flush and exit.
DEFAULT_TERMINATE_GRACE_SECONDS = 5.0

#: Default seconds to wait for the process to be reaped after the unconditional
#: signal. A process wedged in an uninterruptible kernel wait can outlast even
#: SIGKILL, which is why this is bounded and why failure to reap is reported
#: rather than waited on forever.
DEFAULT_KILL_GRACE_SECONDS = 2.0

#: Default seconds to wait for the output readers to finish after the process is
#: reaped. A surviving orphan can hold the write end of the pipe open, so this
#: cannot be unbounded either.
DEFAULT_DRAIN_GRACE_SECONDS = 2.0

#: How often the wait loop wakes to re-check the deadline and the cancel signal.
_POLL_SECONDS = 0.02

_READ_CHUNK = 65536


def process_group_supported() -> bool:
    """Whether this platform can start and signal a dedicated process group.

    True on POSIX, where ``start_new_session`` and :func:`os.killpg` both exist.
    False on Windows, where neither does.
    """
    return os.name == "posix" and hasattr(os, "killpg") and hasattr(os, "setsid")


def _orphan_prevention() -> str:
    """The key into :data:`ORPHAN_PREVENTION` that describes this platform."""
    return "process-group" if process_group_supported() else "direct-child-only"


class _Drain(threading.Thread):
    """Reads one pipe to EOF, keeping a bounded prefix and counting the whole.

    Both halves matter. Reading to EOF regardless of the bound is what stops a
    chatty process from deadlocking on a full pipe buffer while the runner waits
    for an exit that cannot happen. Counting past the bound is what lets the
    outcome say how much was dropped rather than only that something was.
    """

    def __init__(self, stream: Any, limit: int | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._chunks: list[bytes] = []
        self._kept = 0
        self.total = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(_READ_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                if self._limit is None:
                    self._chunks.append(chunk)
                    continue
                room = self._limit - self._kept
                if room <= 0:
                    self.truncated = True
                    continue
                if len(chunk) > room:
                    self.truncated = True
                    chunk = chunk[:room]
                self._chunks.append(chunk)
                self._kept += len(chunk)
        except (ValueError, OSError):
            # The pipe was closed underneath us during teardown. Whatever was
            # read stays; the caller learns the stream is a prefix from
            # ``output_complete``.
            pass

    def text(self) -> str:
        # Decoded once at the end. Truncation cuts on a byte boundary and may
        # split a multi-byte character, which "replace" turns into a visible
        # replacement rather than an exception.
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _signal_target(process: subprocess.Popen[bytes], group: bool) -> int:
    if group:
        return os.getpgid(process.pid)
    return process.pid


def _send(process: subprocess.Popen[bytes], sig: int, group: bool) -> bool:
    """Deliver *sig*, to the process group when there is one. False if already gone."""
    try:
        if group:
            os.killpg(_signal_target(process, True), sig)
        else:
            process.send_signal(sig)
    except OSError:
        return False
    return True


def _terminate(
    process: subprocess.Popen[bytes],
    *,
    reason: str,
    grace: float,
    kill_grace: float,
) -> Termination:
    """Escalate from a polite signal to an unconditional one, and record the path.

    The escalation is deliberately two-stage and deliberately bounded at both
    stages. A single immediate kill loses every chance a program has to flush its
    output; an unbounded wait for a graceful exit turns a timeout into a hang.
    """
    group = process_group_supported()
    orphan_prevention = _orphan_prevention()
    steps: list[str] = []

    polite = signal.SIGTERM
    unconditional = getattr(signal, "SIGKILL", signal.SIGTERM)
    # On Windows both Popen.terminate and Popen.kill are TerminateProcess, so the
    # names below describe the platform call actually made rather than implying a
    # POSIX signal that was never sent.
    polite_name = "SIGTERM" if group else "TerminateProcess"
    unconditional_name = "SIGKILL" if group else "TerminateProcess"

    _send(process, polite, group)
    steps.append(polite_name)
    try:
        process.wait(timeout=grace)
        return Termination(
            reason=reason,
            steps=tuple(steps),
            escalated=False,
            completed=True,
            group_signalled=group,
            orphan_prevention=orphan_prevention,
        )
    except subprocess.TimeoutExpired:
        pass

    _send(process, unconditional, group)
    steps.append(unconditional_name)
    try:
        process.wait(timeout=kill_grace)
        completed = True
    except subprocess.TimeoutExpired:
        completed = False

    return Termination(
        reason=reason,
        steps=tuple(steps),
        escalated=True,
        completed=completed,
        group_signalled=group,
        orphan_prevention=orphan_prevention,
    )


def _default_shell() -> list[str]:
    """The shell a raw ``command`` string is handed to.

    Spelled as an explicit argv rather than ``shell=True`` so the interpreter is
    named in the process vector instead of chosen inside subprocess, and so the
    same call site can carry ``start_new_session``.
    """
    if os.name == "nt":  # pragma: no cover - POSIX-only test environment
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    return ["/bin/sh", "-c"]


@dataclass(frozen=True)
class HostRunner:
    """Runs operations directly on the operator's machine.

    Satisfies :class:`shell.runners.Runner`. Holds no state beyond its own
    self-description and its termination timings: the roots, limits and policies
    an operation runs under belong to the :class:`~shell.environment.Environment`,
    not to the runner, so that the workspace axis and the runner axis stay
    independent.
    """

    name: str = "host"
    isolation: str = "none"
    isolation_note: str = HOST_ISOLATION_NOTE

    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS
    drain_grace_seconds: float = DEFAULT_DRAIN_GRACE_SECONDS

    def describe(self) -> dict[str, Any]:
        """JSON-serializable description, including the honest posture."""
        orphan_prevention = _orphan_prevention()
        return {
            "name": self.name,
            "isolation": self.isolation,
            "isolation_note": self.isolation_note,
            "platform": sys.platform,
            "process_groups": process_group_supported(),
            "orphan_prevention": orphan_prevention,
            "orphan_prevention_note": ORPHAN_PREVENTION[orphan_prevention],
            "terminate_grace_seconds": self.terminate_grace_seconds,
            "kill_grace_seconds": self.kill_grace_seconds,
        }

    def run_process(
        self,
        request: ProcessRequest,
        *,
        cancel: threading.Event | None = None,
    ) -> ProcessOutcome:
        """Run one process to completion, a deadline, or a cancellation.

        Never raises for anything the command does — a failure to start becomes
        an outcome with ``error`` set and ``exit_code`` ``None``, because the
        caller is a pipeline that has to record *something* for every operation.

        *cancel* is polled, not delivered: setting it asks the runner to stop
        waiting and start the same escalation a timeout would. The escalation
        path taken is on ``outcome.termination`` either way, including which
        signals were sent and whether the process was actually reaped.
        """
        started_at = time.time()
        argv = self._argv(request)
        cwd = None if request.cwd is None else str(request.cwd)

        try:
            process = self._spawn(request, argv)
        except (OSError, ValueError) as exc:
            return ProcessOutcome(
                exit_code=None,
                started_at=started_at,
                ended_at=time.time(),
                cwd=cwd,
                error=f"{type(exc).__name__}: {exc}",
            )

        drains = self._start_drains(process, request.max_output_bytes)
        self._feed_stdin(process, request.stdin)

        reason = self._wait(process, request.timeout_seconds, cancel)
        if reason == TerminationReason.NONE:
            # Nothing was signalled, so ``group_signalled`` stays False — it
            # reports what was *done*, not what could have been. The platform's
            # capability is still published, because a consumer comparing two
            # outcomes needs it on both.
            termination = Termination(orphan_prevention=_orphan_prevention())
        else:
            termination = _terminate(
                process,
                reason=reason,
                grace=self.terminate_grace_seconds,
                kill_grace=self.kill_grace_seconds,
            )

        output_complete = self._drain_out(drains)
        stdout, stderr = drains

        return ProcessOutcome(
            exit_code=process.poll(),
            stdout=stdout.text(),
            stderr=stderr.text(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            stdout_bytes=stdout.total,
            stderr_bytes=stderr.total,
            timed_out=reason == TerminationReason.TIMEOUT,
            cancelled=reason == TerminationReason.CANCELLED,
            started_at=started_at,
            ended_at=time.time(),
            cwd=cwd,
            termination=termination,
            output_complete=output_complete,
        )

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _argv(request: ProcessRequest) -> list[str]:
        if request.command is not None:
            return [*_default_shell(), request.command]
        return list(request.argv)

    @staticmethod
    def _spawn(request: ProcessRequest, argv: list[str]) -> subprocess.Popen[bytes]:
        """Start *argv* in its own process group where the platform has them.

        ``start_new_session`` is silently ignored on Windows rather than raising,
        so the fallback is a real behavioural difference and not a crash. That is
        exactly why ``orphan_prevention`` is reported as data.
        """
        kwargs: dict[str, Any] = {
            "cwd": request.cwd,
            "stdin": subprocess.PIPE if request.stdin is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if request.env is not None:
            kwargs["env"] = dict(request.env)
        if process_group_supported():
            kwargs["start_new_session"] = True
        # No shell=True anywhere: a raw command string is turned into an explicit
        # ["/bin/sh", "-c", command] vector by _argv, so the interpreter is named
        # in the vector and the same spawn path carries start_new_session.
        return subprocess.Popen(argv, **kwargs)

    @staticmethod
    def _start_drains(process: subprocess.Popen[bytes], limit: int | None) -> tuple[_Drain, _Drain]:
        drains = (_Drain(process.stdout, limit), _Drain(process.stderr, limit))
        for drain in drains:
            drain.start()
        return drains

    @staticmethod
    def _feed_stdin(process: subprocess.Popen[bytes], stdin: str | None) -> None:
        if process.stdin is None:
            return
        try:
            if stdin:
                process.stdin.write(stdin.encode("utf-8"))
            process.stdin.close()
        except OSError:
            # The command exited or closed its input before reading it. That is
            # the command's business, not an error in running it.
            pass

    @staticmethod
    def _wait(
        process: subprocess.Popen[bytes],
        timeout_seconds: float | None,
        cancel: threading.Event | None,
    ) -> str:
        """Wait for exit, returning why waiting stopped.

        Polls rather than handing the deadline to ``Popen.wait`` because the
        cancel signal has to be observed on the same loop, and because
        ``subprocess``'s own timeout path kills only the direct child — the very
        behaviour this runner exists to replace.
        """
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            if cancel is not None and cancel.is_set():
                return TerminationReason.CANCELLED
            slice_seconds = _POLL_SECONDS
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TerminationReason.TIMEOUT
                slice_seconds = min(_POLL_SECONDS, remaining)
            try:
                process.wait(timeout=slice_seconds)
                return TerminationReason.NONE
            except subprocess.TimeoutExpired:
                continue

    def _drain_out(self, drains: tuple[_Drain, _Drain]) -> bool:
        """Join the readers on a bound, reporting whether they finished.

        The bound is not optional. A descendant that escaped the process group
        keeps the write end of the pipe open, so the readers never see EOF and an
        unbounded join would hang the operation after the process it was running
        is already gone. When that happens the captured streams are a prefix, and
        the outcome says so instead of presenting them as the whole output.
        """
        for drain in drains:
            drain.join(timeout=self.drain_grace_seconds)
        return not any(drain.is_alive() for drain in drains)
