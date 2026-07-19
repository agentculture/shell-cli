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

Scope: this module is the skeleton — identity, posture, and the evidence seam.
Process execution semantics (argv vectors, environment scrubbing, process
groups, timeout escalation, output bounding) land in the following slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["HostRunner"]

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


@dataclass(frozen=True)
class HostRunner:
    """Runs operations directly on the operator's machine.

    Satisfies :class:`shell.runners.Runner`. Holds no state beyond its own
    self-description: the roots, limits and policies an operation runs under
    belong to the :class:`~shell.environment.Environment`, not to the runner, so
    that the workspace axis and the runner axis stay independent.
    """

    name: str = "host"
    isolation: str = "none"
    isolation_note: str = HOST_ISOLATION_NOTE

    def describe(self) -> dict[str, Any]:
        """JSON-serializable description, including the honest posture."""
        return {
            "name": self.name,
            "isolation": self.isolation,
            "isolation_note": self.isolation_note,
        }
