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

The protocol is deliberately narrow. Process execution semantics — argv
handling, process groups, timeout escalation, output bounding — are the next
slice's work; adding a ``run_process`` member here that the only implementation
raises on would be an abstraction with no consumer and a lie about the shape.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["Runner"]


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
