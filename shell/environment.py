"""The environment an operation runs in: two independent axes, never one mode.

"Host", "worktree" and "container" are not three values of one setting. They are
points on two axes that vary independently, and collapsing them into a single
overloaded mode is the mistake this module exists to prevent:

* the **workspace axis** — which roots exist and which of them may change
  (:attr:`Environment.source_root`, :attr:`Environment.work_root`,
  :attr:`Environment.workspace`);
* the **runner axis** — what carries the work out (:attr:`Environment.runner`;
  see :mod:`shell.runners`).

All four combinations are meaningful, and each carries a different guarantee. A
worktree on the host gives reviewable, recoverable changes and no process
isolation whatsoever; a checkout in a container (Milestone 4) gives the reverse.

**The two roots are not interchangeable.** ``source_root`` is trusted control
context — it is where an operator's policy is read from. ``work_root`` is what
operations may observe and change. Policy is snapshotted from the source root
*before* model-driven mutation, precisely so that an agent cannot edit its own
active authorization by writing a file inside the work root. Deployments where
the two roots are the same directory get no such separation, and that is a
property of the deployment, not a defect here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from shell.results import SCHEMA_VERSION
from shell.runners import Runner

__all__ = [
    "Environment",
    "NetworkPolicy",
    "WorkspaceKind",
]

#: Conservative defaults. The output bound matches the 25000-character limit the
#: first consumer already applies, so migrating callers see no change in
#: truncation behaviour.
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_BYTES = 25_000


class WorkspaceKind(str, Enum):
    """Which kind of working tree the work root is.

    Recorded on evidence because it changes what a *reviewer* can recover, not
    what a process can reach: a worktree makes changes reviewable and
    discardable. It contains nothing on its own.
    """

    CHECKOUT = "checkout"
    WORKTREE = "worktree"


class NetworkPolicy(str, Enum):
    """The declared network posture for operations in this environment.

    ``UNRESTRICTED`` is the honest default for a runner that enforces nothing.
    ``DENY`` and ``ALLOW`` are *declarations*: they are recorded on evidence and
    become real only under a runner that can enforce them, which the host runner
    cannot. :attr:`Environment.network_enforced` reports which case applies, so
    a consumer never mistakes a declaration for a control.
    """

    UNRESTRICTED = "unrestricted"
    DENY = "deny"
    ALLOW = "allow"


def _resolve(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class Environment:
    """Where and under what constraints operations run.

    Constructing one takes an explicit runner. There is no default: an
    environment that silently selects host execution is exactly the kind of
    implicit choice that later reads as a deliberate one.
    """

    source_root: Path
    work_root: Path
    runner: Runner

    workspace: WorkspaceKind = WorkspaceKind.CHECKOUT

    #: Paths inside the work root that operations may read but never write.
    #: Generalised from the first consumer's single hard-coded clone directory.
    read_only_paths: tuple[Path, ...] = ()

    #: Declared mounts, for runners that have any. The host runner declares
    #: none — not because nothing is reachable, but because everything is: a
    #: host process sees the whole filesystem, which is a posture, not a mount
    #: list.
    mounts: tuple[str, ...] = ()

    network: NetworkPolicy = NetworkPolicy.UNRESTRICTED

    #: Names of host environment variables a process may inherit. Empty means
    #: minimal inheritance — an allow-list, never a deny-list.
    env_passthrough: tuple[str, ...] = ()

    #: Names of secrets this environment is willing to inject. Names only:
    #: values never live on this object and are never recorded in evidence.
    secret_names: tuple[str, ...] = ()

    #: Identity operations run as, when the runner can select one.
    user: str | None = None

    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Roots are resolved once, here, so every confinement check downstream
        # compares against a canonical absolute path rather than re-deriving it.
        object.__setattr__(self, "source_root", _resolve(self.source_root))
        object.__setattr__(self, "work_root", _resolve(self.work_root))
        object.__setattr__(
            self, "read_only_paths", tuple(_resolve(p) for p in self.read_only_paths)
        )

    @property
    def network_enforced(self) -> bool:
        """Whether the selected runner can actually enforce :attr:`network`.

        False for host execution, where the declared policy is a record of
        intent and nothing more.
        """
        return getattr(self.runner, "isolation", "none") != "none"

    @property
    def roots_are_separate(self) -> bool:
        """Whether trusted control context is a different tree from the work root."""
        return self.source_root != self.work_root

    def to_dict(self) -> dict[str, Any]:
        runner_description = self.runner.describe()
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_root": str(self.source_root),
            "work_root": str(self.work_root),
            "roots_are_separate": self.roots_are_separate,
            "workspace": self.workspace.value,
            "runner": runner_description,
            "read_only_paths": [str(p) for p in self.read_only_paths],
            "mounts": list(self.mounts),
            "network": self.network.value,
            "network_enforced": self.network_enforced,
            "env_passthrough": list(self.env_passthrough),
            "secret_names": list(self.secret_names),
            "user": self.user,
            "default_timeout_seconds": self.default_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }
