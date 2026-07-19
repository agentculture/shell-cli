"""The runner axis of an environment: *where* an operation's work happens.

A runner is one half of the environment contract; the workspace (which roots are
readable and writable) is the other, and the two vary independently. See
:mod:`shell.environment`.

Every runner describes its own isolation posture in its own words, and that
description travels on every result's evidence. A consumer therefore never has
to infer what protection it got from the runner's *name*:

* :class:`~shell.runners.host.HostRunner` reports ``isolation="none"`` — it is a
  guard, not a sandbox.
* A container runner (Milestone 4) will report a declared boundary with a
  documented profile. That is a separate claim about a separate mechanism; it
  does not retroactively upgrade the host path, and one sentence must never
  cover both.

The protocol stays narrow: it declares only what an environment genuinely needs.
``run_process`` is here because there is now an implementation that satisfies it
end to end — argv handling, process groups, timeout and cancellation escalation,
bounded output. Its signature is deliberately free of host-specific vocabulary
(no session flags, no signal numbers) so a container runner can satisfy the same
contract by wrapping the request rather than by widening it.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from shell.runners.types import ProcessOutcome, ProcessRequest

__all__ = ["ProcessOutcome", "ProcessRequest", "Runner"]


@runtime_checkable
class Runner(Protocol):
    """What an environment needs from whatever will carry out its operations."""

    #: Stable identifier recorded as ``Evidence.backend`` (e.g. ``"host"``).
    name: str

    #: The runner's own isolation self-description, recorded verbatim as
    #: ``Evidence.isolation``. ``"none"`` is a legitimate and expected value.
    isolation: str

    #: Prose stating what the isolation value does and does not mean, recorded
    #: as ``Evidence.isolation_note``. It must not overstate the protection.
    isolation_note: str

    def describe(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this runner."""
        ...  # pragma: no cover - protocol declaration

    def run_process(
        self,
        request: ProcessRequest,
        *,
        cancel: threading.Event | None = None,
    ) -> ProcessOutcome:
        """Run one process to completion, to its deadline, or to a cancellation.

        Implementations do not raise for anything the command does. A command
        that exits non-zero, is cut off by its timeout, or cannot be started at
        all is an *outcome*, because the caller is a pipeline that must record
        something for every operation and an exception would lose it.

        *cancel* is polled, not delivered. Setting it asks the runner to stop
        waiting and begin the same termination escalation a timeout would, and
        the path that escalation actually took is reported on
        ``outcome.termination`` in both cases.
        """
        ...  # pragma: no cover - protocol declaration
