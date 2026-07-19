"""The operation: the core abstraction, and the one lifecycle every kind follows.

An **operation** is any local observation, mutation, process invocation, or
environment lifecycle action. The boundary is *work-affecting I/O* — not every
internal byte a runtime writes. Runtime-private bookkeeping (caches, trace feeds,
telemetry buffers, lock files) stays with its owning runtime unless it executes
code or touches the target workspace.

Every operation goes through exactly one pipeline::

    intent -> Operation -> policy + preview -> environment backend
           -> result + evidence -> caller

There is no second path. The CLI is a front end over this same function, never a
parallel implementation, and handlers stay small: a handler receives a
normalized :class:`Operation` and an :class:`~shell.environment.Environment` and
returns an :class:`~shell.results.OperationResult`. The lifecycle — normalize,
gate, preview, time, stamp evidence, contain crashes — lives here once.

Two orderings in :func:`execute` are load-bearing.

**The policy gate runs before the preview branch.** A caller asking to preview a
command the operator has denied is told it is denied, not handed a preview that
implies it would otherwise run.

**A handler crash becomes a failed result, not an exception.** The first
consumer drives a model in a loop; an unhandled exception aborts the whole drive,
whereas a ``FAILED`` result is a recoverable, model-visible step the agent can
react to. Malformed arguments must behave the same way.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping

from shell.environment import Environment
from shell.results import (
    SCHEMA_VERSION,
    Effects,
    Evidence,
    OperationResult,
    OperationStatus,
    PolicyDecision,
    PolicyVerdict,
)

__all__ = [
    "ExecutionProfile",
    "Operation",
    "OperationIntent",
    "UnknownOperationKind",
    "execute",
    "normalize",
    "register",
    "registered_kinds",
]


class OperationIntent(str, Enum):
    """What an operation does to the world.

    This is what decides whether the operation previews by default, so it is not
    a free-form label: it is declared by the handler at registration and a caller
    cannot relabel a mutation as an observation to slip past the preview gate
    (see :func:`normalize`).
    """

    OBSERVE = "observe"
    MUTATE = "mutate"
    EXECUTE = "execute"
    LIFECYCLE = "lifecycle"


class ExecutionProfile(str, Enum):
    """Why a subprocess is running — not all of them deserve equal trust.

    ``PROJECT`` executes repository-controlled code: model-issued commands,
    tests, linters, repo hooks. ``CONTROL`` executes trusted control-plane
    programs — git mechanics, capability CLIs — for which raw shell strings are
    never appropriate. ``OBSERVE`` is structured reads, confined to the selected
    root; it never implies process isolation.
    """

    PROJECT = "project"
    CONTROL = "control"
    OBSERVE = "observe"


#: Intents that must not take effect unless the caller explicitly applied. Reads
#: are absent on purpose: previewing a read would be theatre.
_REQUIRES_APPLY = frozenset(
    {OperationIntent.MUTATE, OperationIntent.EXECUTE, OperationIntent.LIFECYCLE}
)


class UnknownOperationKind(LookupError):
    """Raised by :func:`normalize` for a kind no handler has registered."""


@dataclass(frozen=True)
class Operation:
    """One requested local operation, fully described and JSON-serializable.

    ``apply`` defaults to ``False``, and that default is the contract: an
    imported caller must state ``apply=True`` explicitly, because agents call
    this in loops and an implicit apply turns a mistaken plan into a mistaken
    change. Reads are unaffected — their intent exempts them.

    ``intent`` and ``profile`` may be left unset; :func:`normalize` fills them
    from the handler's registration. Supplying one that contradicts the
    registration is an error rather than an override.

    ``timeout_seconds`` and ``max_output_bytes`` are the resource request. ``None``
    means "use the environment's default" — see :meth:`resolved_timeout` and
    :meth:`resolved_max_output_bytes`. Container-only limits (cpu, memory, pids)
    join them when a runner exists that can apply them.
    """

    kind: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    intent: OperationIntent | None = None
    profile: ExecutionProfile | None = None
    apply: bool = False
    #: Provenance: which agent, task and semantic tool asked for this. Free-form
    #: on purpose — the caller owns these semantics, shell-cli only records them.
    caller: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    max_output_bytes: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = SCHEMA_VERSION

    @property
    def requires_apply(self) -> bool:
        """Whether this operation previews unless the caller applied.

        An operation whose intent is not yet resolved is treated as requiring
        apply: unknown is the conservative side of this question.
        """
        return self.intent is None or self.intent in _REQUIRES_APPLY

    def resolved_timeout(self, environment: Environment) -> float:
        if self.timeout_seconds is None:
            return environment.default_timeout_seconds
        return self.timeout_seconds

    def resolved_max_output_bytes(self, environment: Environment) -> int:
        if self.max_output_bytes is None:
            return environment.max_output_bytes
        return self.max_output_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "arguments": dict(self.arguments),
            "intent": None if self.intent is None else self.intent.value,
            "profile": None if self.profile is None else self.profile.value,
            "apply": self.apply,
            "caller": dict(self.caller),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Operation:
        """Rebuild an operation from :meth:`to_dict` output.

        The inbound half of the cross-repo contract. Unknown ``schema_version``
        values are *not* silently coerced — the caller compares versions and
        decides, because guessing at a shape change is how skew becomes a
        security bug rather than an error message.
        """
        intent = payload.get("intent")
        profile = payload.get("profile")
        return cls(
            kind=payload["kind"],
            arguments=dict(payload.get("arguments") or {}),
            intent=None if intent is None else OperationIntent(intent),
            profile=None if profile is None else ExecutionProfile(profile),
            apply=bool(payload.get("apply", False)),
            caller=dict(payload.get("caller") or {}),
            timeout_seconds=payload.get("timeout_seconds"),
            max_output_bytes=payload.get("max_output_bytes"),
            id=payload.get("id") or uuid.uuid4().hex,
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
        )


Handler = Callable[[Operation, Environment], OperationResult]


@dataclass(frozen=True)
class HandlerSpec:
    """A registered operation kind: what it does, and who carries it out."""

    kind: str
    intent: OperationIntent
    default_profile: ExecutionProfile
    run: Handler


_HANDLERS: dict[str, HandlerSpec] = {}


def register(
    kind: str,
    *,
    intent: OperationIntent,
    default_profile: ExecutionProfile,
    run: Handler,
) -> HandlerSpec:
    """Register the handler for *kind*.

    The intent is declared here, once, by the code that actually performs the
    work — not by whoever calls it. That is what makes the preview gate
    trustworthy.
    """
    if kind in _HANDLERS:
        raise ValueError(f"operation kind is already registered: {kind!r}")
    spec = HandlerSpec(kind=kind, intent=intent, default_profile=default_profile, run=run)
    _HANDLERS[kind] = spec
    return spec


def unregister(kind: str) -> None:
    """Remove a registration. Intended for tests that install a fake handler."""
    _HANDLERS.pop(kind, None)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def handler_for(kind: str) -> HandlerSpec:
    try:
        return _HANDLERS[kind]
    except KeyError:
        raise UnknownOperationKind(f"no handler registered for operation kind {kind!r}") from None


def normalize(operation: Operation) -> Operation:
    """Return *operation* with intent and profile resolved from the registry.

    Raises :class:`UnknownOperationKind` for an unregistered kind, and
    :class:`ValueError` when the caller supplied an intent that contradicts the
    handler's declaration — a caller must not be able to describe a mutation as
    an observation and thereby skip the preview gate.
    """
    spec = handler_for(operation.kind)

    if operation.intent is not None and operation.intent is not spec.intent:
        raise ValueError(
            f"operation kind {operation.kind!r} is declared {spec.intent.value!r}, "
            f"but the caller supplied {operation.intent.value!r}"
        )

    return replace(
        operation,
        intent=spec.intent,
        profile=operation.profile if operation.profile is not None else spec.default_profile,
    )


def _policy_gate(operation: Operation, environment: Environment) -> PolicyVerdict:
    """Evaluate the operation against the operator's policy snapshot.

    Seam only. The policy evaluator, its file format and the snapshotting of it
    from trusted control context are the next slice; until then every operation
    is reported ``UNGATED`` — which is deliberately distinct from ``ALLOWED``, so
    a consumer can tell "no gate exists yet" from "a gate permitted this".
    """
    return PolicyVerdict(decision=PolicyDecision.UNGATED, reason="no policy evaluator is installed")


def _environment_evidence(environment: Environment) -> Evidence:
    """The environment facts every result carries, whatever its status.

    The runner's isolation posture is copied onto every single result. A consumer
    therefore learns what protection it did or did not get from the result
    itself, rather than inferring it from the runner's name or from prose it may
    never have read.
    """
    runner = environment.runner
    return Evidence(
        backend=getattr(runner, "name", "unknown"),
        isolation=getattr(runner, "isolation", "unknown"),
        isolation_note=getattr(runner, "isolation_note", ""),
        environment_id=environment.id,
        workspace_kind=environment.workspace.value,
        root=str(environment.work_root),
        cwd=str(environment.work_root),
        network=environment.network.value,
        network_enforced=environment.network_enforced,
        mounts=tuple(environment.mounts),
    )


def _stamp(
    result: OperationResult,
    environment: Environment,
    started_at: float,
    ended_at: float,
) -> OperationResult:
    """Overlay environment facts and timing onto a handler's evidence.

    Handlers own what only they can know — exit code, captured streams,
    truncation. Everything about *where* the operation ran is stamped here, so a
    handler cannot accidentally (or conveniently) misreport it.
    """
    base = _environment_evidence(environment)
    handler_evidence = result.evidence
    merged = replace(
        handler_evidence,
        backend=base.backend,
        isolation=base.isolation,
        isolation_note=base.isolation_note,
        environment_id=base.environment_id,
        workspace_kind=base.workspace_kind,
        root=base.root,
        cwd=handler_evidence.cwd or base.cwd,
        network=base.network,
        network_enforced=base.network_enforced,
        mounts=base.mounts,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=(ended_at - started_at) * 1000.0,
    )
    return replace(result, evidence=merged)


def execute(operation: Operation, environment: Environment) -> OperationResult:
    """Run *operation* in *environment* through the single lifecycle pipeline.

    Never raises for an operation-level problem: an unknown kind, a contradictory
    intent, or a handler that crashed all come back as a ``FAILED`` result. The
    caller is an agent loop, and a raised exception ends the loop while a failed
    result is something it can recover from.
    """
    started_at = time.time()

    def _finish(result: OperationResult) -> OperationResult:
        return _stamp(result, environment, started_at, time.time())

    try:
        normalized = normalize(operation)
    except (UnknownOperationKind, ValueError) as exc:
        return _finish(
            OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=str(exc),
                rendering=str(exc),
            )
        )

    # The gate precedes the preview branch on purpose: "this is denied" is more
    # useful, and safer, than a preview implying it would otherwise have run.
    verdict = _policy_gate(normalized, environment)
    if verdict.denied:
        return _finish(
            OperationResult(
                operation_id=normalized.id,
                status=OperationStatus.DENIED,
                verdict=verdict,
                error=verdict.reason,
                rendering=verdict.reason,
            )
        )

    if normalized.requires_apply and not normalized.apply:
        rendering = (
            f"preview: {normalized.kind} was not applied. "
            "Re-issue with apply=True to carry it out."
        )
        return _finish(
            OperationResult(
                operation_id=normalized.id,
                status=OperationStatus.PREVIEWED,
                verdict=verdict,
                rendering=rendering,
                output={"kind": normalized.kind, "arguments": dict(normalized.arguments)},
                # No effects, and no claim of completeness: a preview describes
                # what would run, it does not predict what would change.
                effects=Effects(complete=False),
            )
        )

    spec = handler_for(normalized.kind)
    try:
        result = spec.run(normalized, environment)
    except Exception as exc:  # noqa: BLE001 - a handler crash must stay recoverable
        message = f"{normalized.kind} failed: {type(exc).__name__}: {exc}"
        return _finish(
            OperationResult(
                operation_id=normalized.id,
                status=OperationStatus.FAILED,
                verdict=verdict,
                error=message,
                rendering=message,
            )
        )

    return _finish(replace(result, verdict=verdict))
