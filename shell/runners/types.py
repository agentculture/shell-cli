"""The neutral process contract every runner speaks.

These types live beside the :class:`~shell.runners.Runner` protocol rather than
inside any one runner, because both halves of the runner axis need them and
neither should have to import the other. A container runner (Milestone 4)
satisfies the same contract by *wrapping* a :class:`ProcessRequest` — putting it
behind ``docker run`` — not by extending the shape.

Nothing here names a signal, a session, a namespace or an image. Host-specific
mechanics belong to :mod:`shell.runners.host`; what crosses this seam is only
what was asked for and what happened.

Two commitments are baked into the shapes rather than left to convention:

* **stdout and stderr are separate, and stay separate.** A consumer that wants
  them interleaved concatenates them; the neutral record does not throw away the
  distinction to save that line.
* **Bounds and gaps are reported, never smoothed.** Truncation carries the byte
  count of the original stream, an escalation that failed to reap its process
  says so, and output captured from a pipe still held open by a survivor is
  marked as the prefix it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ORPHAN_PREVENTION",
    "ProcessOutcome",
    "ProcessRequest",
    "Termination",
    "TerminationReason",
]

#: What terminating an operation actually achieves, keyed by the value reported
#: in :attr:`Termination.orphan_prevention`. Held as data so a consumer reads the
#: limit off the payload instead of matching on ``sys.platform`` itself.
#:
#: The two entries are not two ways of saying the same thing. One reduces
#: orphans; the other does not prevent them at all.
ORPHAN_PREVENTION: dict[str, str] = {
    "process-group": (
        "The command leads its own POSIX process group, so termination signals "
        "reach its descendants. A descendant that calls setsid to leave the "
        "group is still out of reach, so this reduces orphans rather than "
        "eliminating them."
    ),
    "direct-child-only": (
        "POSIX process groups are unavailable on this platform, so termination "
        "reaches only the process the runner started. Grandchildren are NOT "
        "terminated and can outlive the operation."
    ),
}


class TerminationReason:
    """Why termination was initiated. Plain strings — this crosses a JSON seam."""

    NONE = ""
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Termination:
    """The escalation path a termination actually took.

    Recorded even when nothing was terminated (``reason`` empty, ``steps``
    empty), so every outcome carries one shape and a consumer never branches on
    presence.

    ``completed`` answers "did the process actually go away?". It is ``False``
    when the escalation ran to its end and the process was still not reaped —
    a real state on POSIX, where a task wedged in an uninterruptible kernel wait
    outlasts even an unconditional kill. It must not be smoothed into a clean
    exit.
    """

    reason: str = TerminationReason.NONE
    #: Signals or platform calls issued, in the order they were issued. This is
    #: the escalation path: reading it tells you what was tried and how far it
    #: got, not merely that something was tried.
    steps: tuple[str, ...] = ()
    #: Whether the polite signal was insufficient and the unconditional one was
    #: sent.
    escalated: bool = False
    completed: bool = True
    #: Whether the signal was addressed to the whole process group or to the
    #: single process the runner started.
    group_signalled: bool = False
    #: ``"process-group"`` or ``"direct-child-only"``. See :data:`ORPHAN_PREVENTION`.
    orphan_prevention: str = "process-group"

    @property
    def terminated(self) -> bool:
        return self.reason != TerminationReason.NONE

    @property
    def note(self) -> str:
        return ORPHAN_PREVENTION.get(self.orphan_prevention, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "terminated": self.terminated,
            "steps": list(self.steps),
            "escalated": self.escalated,
            "completed": self.completed,
            "group_signalled": self.group_signalled,
            "orphan_prevention": self.orphan_prevention,
            "orphan_prevention_note": self.note,
        }


@dataclass(frozen=True)
class ProcessRequest:
    """One process to run, fully resolved by the caller.

    Exactly one of ``argv`` and ``command`` is set, and the difference is not
    cosmetic. ``argv`` is an explicit vector — no shell parses it, so nothing in
    it is re-interpreted. ``command`` is a raw shell string handed to a shell,
    and everything said elsewhere about a gate inspecting a string that a shell
    will later re-interpret applies to it in full.

    ``env`` is deliberately three-valued. ``None`` inherits the calling process's
    environment; a mapping replaces it wholesale; an empty mapping means a
    genuinely empty environment. A runner applies no passthrough policy of its
    own — that belongs to the environment and its handler, and a runner quietly
    adding ``PATH`` back would make the caller's declared policy a fiction.
    """

    argv: tuple[str, ...] = ()
    command: str | None = None
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    #: Wall-clock bound in seconds. ``None`` means unbounded, which callers
    #: arriving through the operation pipeline never are:
    #: ``Operation.resolved_timeout`` always yields a number.
    timeout_seconds: float | None = None
    #: Bytes retained per stream. Overflow is counted, then discarded — so a
    #: truncated stream still reports how much was produced.
    max_output_bytes: int | None = None
    stdin: str | None = None

    def __post_init__(self) -> None:
        if bool(self.argv) == (self.command is not None):
            raise ValueError("a process request sets exactly one of argv and command")
        if self.command is not None and not self.command.strip():
            raise ValueError("command must not be blank")

    @property
    def uses_shell(self) -> bool:
        return self.command is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON form. Environment *names* only — values are never recorded."""
        return {
            "argv": list(self.argv),
            "command": self.command,
            "uses_shell": self.uses_shell,
            "cwd": None if self.cwd is None else str(self.cwd),
            "env_names": None if self.env is None else sorted(self.env),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "stdin_bytes": None if self.stdin is None else len(self.stdin.encode("utf-8")),
        }


@dataclass(frozen=True)
class ProcessOutcome:
    """What running one process produced. Neutral, JSON-serializable, no verdict.

    ``stdout_bytes`` / ``stderr_bytes`` count the *original* stream, including
    bytes discarded past the bound, so a truncation is reported together with the
    size of what was dropped.

    No digest is produced here, deliberately. A hash is taken downstream from the
    text as finally stored — after redaction and after truncation — because a
    digest of the unredacted stream is a brute-force oracle for a short secret.

    ``timed_out`` is distinct from a non-zero exit. The pipeline maps it to
    ``OperationStatus.TIMED_OUT``, which is a peer of ``FAILED``, not a flavour
    of it: a command that was cut off never reported anything about its work.
    """

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    timed_out: bool = False
    cancelled: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0
    cwd: str | None = None
    termination: Termination = field(default_factory=Termination)
    #: ``False`` when the output readers were still blocked when the runner gave
    #: up waiting for them — typically a survivor holding the write end of the
    #: pipe open. The captured streams are then a prefix of what was produced,
    #: not the whole of it.
    output_complete: bool = True
    #: Set when the process could not be started at all. ``exit_code`` is then
    #: ``None``, which is distinct from an exit code of zero.
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000.0

    @property
    def succeeded(self) -> bool:
        """Exit zero, not cut off, and actually started."""
        return self.exit_code == 0 and not self.timed_out and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "cwd": self.cwd,
            "termination": self.termination.to_dict(),
            "output_complete": self.output_complete,
            "error": self.error,
        }
