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

The full ordering inside :func:`execute` is::

    normalize -> rewrite -> policy gate -> preview branch -> handler -> evidence

Four properties of that ordering are load-bearing.

**The policy gate sees the rewritten arguments, because there is nothing else
left to see.** A caller may supply a ``rewrite`` that adjusts an operation's
arguments before it runs — the first consumer's ``pre_tool`` hook is exactly
this. Gating the *original* while running the *rewritten* form is the classic
bypass: a rewrite could turn a denied command into an allowed shape, or an
allowed one into something the operator forbade, and the gate would never know.
This is prevented structurally rather than by discipline. The rewrite produces
one value, ``effective``, and from that point on the pre-rewrite operation is
not in scope for either gating or execution — the same object is gated, previewed
and handed to the handler. There is no second name to accidentally pass.

**A rewrite may change arguments and nothing else.** Identity, kind, intent,
profile and apply-state all pass through untouched; an attempt to change any of
them is a ``FAILED`` result, never an override. Otherwise a rewrite could relabel
``process.shell`` as ``fs.read`` and step out of its own gate's jurisdiction,
which would make the gate advisory. This is the same principle
:func:`normalize` applies to a caller-supplied intent.

**The policy gate runs before the preview branch.** A caller asking to preview a
command the operator has denied is told it is denied, not handed a preview that
implies it would otherwise run.

**A handler crash becomes a failed result, not an exception.** The first
consumer drives a model in a loop; an unhandled exception aborts the whole drive,
whereas a ``FAILED`` result is a recoverable, model-visible step the agent can
react to. Malformed arguments must behave the same way.

Every operation produces an evidence record — including a denied one and a
previewed one. An operation the gate refused that leaves no trace is precisely
the audit gap this package exists to close, so the record is built before the
result is returned regardless of how the operation ended. Persisting it is opt-in
(see the ``evidence_store`` argument); building and delivering it is not.
"""

from __future__ import annotations

import shlex
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping

from shell.environment import Environment
from shell.evidence import EvidenceRecord, EvidenceStore, HandlerDisposition, capture
from shell.policy import Policy
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
    "GATED_KIND_PREFIXES",
    "ExecutionProfile",
    "Operation",
    "OperationIntent",
    "RewriteRejected",
    "UnknownOperationKind",
    "apply_rewrite",
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


class RewriteRejected(ValueError):
    """A rewrite tried to change something other than the operation's arguments.

    Raised by :func:`apply_rewrite`. It is an error rather than a silently
    ignored change because the two are not equivalent: ignoring it would run an
    operation the rewriter believed it had altered, and the rewriter is the
    consumer's authorization-adjacent hook.
    """


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


#: Kind prefixes the ``run_command`` policy has authority over. Everything else
#: is outside its jurisdiction — see :func:`_policy_gate` on why that carve-out
#: is preserved deliberately rather than closed.
GATED_KIND_PREFIXES = ("process.",)


#: What a rewrite may return: replacement arguments, a whole replacement
#: operation, or ``None`` for "leave it alone". The mapping form is the shape the
#: first consumer's hook already produces, and it is the safer of the two by
#: construction — a rewrite that can only return arguments has no channel through
#: which to express a kind change at all. The operation form is accepted so that
#: an attempt to change anything else is a reported error rather than an
#: unrepresentable one.
Rewrite = Callable[["Operation"], "Mapping[str, Any] | Operation | None"]


def apply_rewrite(
    operation: Operation, candidate: Mapping[str, Any] | Operation | None
) -> Operation:
    """Fold a rewrite's *candidate* result into *operation*, arguments only.

    Returns *operation* unchanged when *candidate* is ``None``. A mapping becomes
    the new arguments. A whole :class:`Operation` is accepted only when every
    field except ``arguments`` is identical to the original — including ``id``,
    because the operation id is the key the evidence record is filed under and a
    rewrite that mints a new one detaches the record from what the caller asked
    for.

    Raises :class:`RewriteRejected` otherwise. ``kind`` is checked first and
    reported on its own: a kind change is not a stricter version of the same
    mistake, it is an attempt to leave one gate's jurisdiction for another's.
    """
    if candidate is None:
        return operation

    if not isinstance(candidate, Operation):
        return replace(operation, arguments=dict(candidate))

    if candidate.kind != operation.kind:
        raise RewriteRejected(
            f"a rewrite may not change the operation kind: {operation.kind!r} -> "
            f"{candidate.kind!r}. Arguments are rewritable; the kind selects which "
            "policy has authority over the operation and is not."
        )

    # Everything but ``arguments`` must survive the rewrite untouched. Comparing
    # two argument-blanked copies covers every field at once, so a field added to
    # ``Operation`` later is protected without anyone remembering to list it.
    if replace(candidate, arguments={}) != replace(operation, arguments={}):
        raise RewriteRejected(
            f"a rewrite of {operation.kind!r} may change arguments only; identity, "
            "intent, profile, apply-state and resource limits are not rewritable"
        )

    return candidate


def _gated_command(operation: Operation) -> str:
    """The command string the ``run_command`` policy should judge this by.

    A raw shell string is judged as written. An argv vector is joined with
    :func:`shlex.join` so that re-tokenizing it recovers ``argv[0]`` — the
    program token — which is what the gate actually inspects.

    An operation carrying neither yields an empty string, and the policy decides
    what that means. Under a *present* section an empty command has no program
    token to approve and is denied; under an absent one it stays ungated. Both
    are the policy's calls to make, not this function's.
    """
    command = operation.arguments.get("command")
    if isinstance(command, str) and command.strip():
        return command

    argv = operation.arguments.get("argv")
    if isinstance(argv, (list, tuple)) and argv:
        return shlex.join(str(part) for part in argv)

    return ""


def _policy_gate(operation: Operation, policy: Policy) -> PolicyVerdict:
    """Evaluate *operation* against the operator's policy snapshot.

    Jurisdiction first. Only kinds under :data:`GATED_KIND_PREFIXES` are subject
    to the ``run_command`` gate; structured filesystem operations are deliberately
    **not** routed through it. That carve-out is inherited from the first consumer
    and is preserved rather than fixed: confining file operations is the
    filesystem layer's job, and running every read through a command allow-list
    would be a different product.

    **An untrustworthy policy fails closed — inside its own jurisdiction only.**
    A degraded or unresolved snapshot means the operator declared a gate that
    could not be read, and treating that as permission is exactly how a malformed
    file becomes an accidental allow-all. So a gated kind is denied. A carved-out
    kind is *not* denied by it: no policy file, however well-formed, could have
    gated that operation, so refusing it would be enforcing a rule that could
    never have existed. The trust note still rides on the verdict either way, so
    the degradation is never silent.

    This gate inspects operator intent. It is not containment: for a raw shell
    string it reads the first token of something a shell re-interprets later, and
    ``sh -c``, pipelines and an absolute path to a renamed binary all step around
    it. Real containment lives on the runner axis, never here.
    """
    gated = operation.kind.startswith(GATED_KIND_PREFIXES)

    if not gated:
        reason = f"{operation.kind!r} is not subject to the run_command policy"
        if policy.trust_note:
            reason = f"{reason} [policy degraded: {policy.trust_note}]"
        return PolicyVerdict(decision=PolicyDecision.UNGATED, reason=reason)

    if not policy.trustworthy:
        return PolicyVerdict(
            decision=PolicyDecision.DENIED,
            reason=(
                f"{operation.kind} denied: the policy snapshot could not be trusted, "
                "so the gate fails closed rather than reading an unreadable policy "
                f"as permission [policy degraded: {policy.trust_note}]"
            ),
            matched_rule="policy.untrustworthy",
        )

    return policy.check_run_command(_gated_command(operation))


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


def _record_evidence(
    result: OperationResult,
    *,
    requested: Operation,
    effective: Operation | None,
    environment: Environment,
    store: EvidenceStore | None,
    secrets: Mapping[str, str] | None,
    sink: Callable[[EvidenceRecord], None] | None,
    disposition: HandlerDisposition,
) -> OperationResult:
    """Build, optionally persist and optionally deliver the record for *result*.

    Runs for every terminal state, denied and previewed included. Returns the
    result to hand onward — which is the *same* result unless persistence failed,
    in which case it carries ``evidence.degraded`` and a reason, because an
    action that happened and could not be recorded must not read as a clean run.

    Bookkeeping never overturns an outcome. If the record itself cannot be built
    or delivered, the operation's own status stands and the failure is reported
    on the evidence rather than by raising: the operation already happened, and
    replacing its result with an exception about the paperwork would lose it.
    """
    try:
        recorded, record = capture(
            result,
            requested=requested,
            normalized=effective,
            environment=environment,
            store=store,
            secrets=secrets,
            disposition=disposition,
        )
    except Exception as exc:  # noqa: BLE001 - evidence failure must not eat a result
        reason = f"evidence record could not be built: {type(exc).__name__}: {exc}"
        return replace(result, evidence=_degraded(result.evidence, reason))

    if sink is None:
        return recorded

    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 - a consumer's sink is not trusted to behave
        reason = f"evidence sink raised: {type(exc).__name__}: {exc}"
        return replace(recorded, evidence=_degraded(recorded.evidence, reason))

    return recorded


def _degraded(evidence: Evidence, reason: str) -> Evidence:
    """Mark *evidence* degraded, keeping any reason already recorded."""
    combined = f"{evidence.degraded_reason}; {reason}" if evidence.degraded_reason else reason
    return replace(evidence, degraded=True, degraded_reason=combined)


def execute(
    operation: Operation,
    environment: Environment,
    *,
    policy: Policy | None = None,
    rewrite: Rewrite | None = None,
    evidence_store: EvidenceStore | None = None,
    evidence_sink: Callable[[EvidenceRecord], None] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> OperationResult:
    """Run *operation* in *environment* through the single lifecycle pipeline.

    This is the only path. A caller that reaches this function is gated by it —
    the policy evaluation is *inside* here, not in a wrapper someone can call
    around it, so there is no arrangement of imports under which an operation
    executes without a verdict attached to its result.

    ``policy`` is the operator's snapshot (see :func:`shell.policy.snapshot`).
    ``None`` means no policy was configured and is treated exactly like an empty
    one: every operation comes back ``UNGATED``, which is deliberately not
    ``ALLOWED``. Passing no policy is therefore indistinguishable from declaring
    nothing — it is not a way to turn a configured gate off, because the gate a
    caller would be turning off is the one it also had to supply.

    ``rewrite`` is called with the normalized operation and may return
    replacement arguments, a whole operation identical but for its arguments, or
    ``None``. Whatever it produces is what gets gated *and* what gets run; see
    the module docstring on why those cannot be two different values here.

    ``evidence_store`` persists the record; without one the record is built and
    handed to ``evidence_sink`` (when given) but not written to disk. Persistence
    is opt-in on purpose — silently creating files under an operator's source root
    on the first call would be a side effect nobody asked for — so a consumer that
    wants a durable audit trail must configure a store and is told plainly here
    that not configuring one means there is no trail.

    Never raises for an operation-level problem: an unknown kind, a contradictory
    intent, a rejected rewrite, or a handler that crashed all come back as a
    ``FAILED`` result. The caller is an agent loop, and a raised exception ends
    the loop while a failed result is something it can recover from.
    """
    started_at = time.time()
    policy = policy if policy is not None else Policy()

    # Every exit from this function states how far it got. The parameter is
    # required rather than defaulted: a defaulted disposition is a value someone
    # forgets to pass, and the forgotten case would be recorded as a claim about
    # the world instead of an omission.
    def _finish(
        result: OperationResult,
        effective: Operation | None,
        disposition: HandlerDisposition,
    ) -> OperationResult:
        stamped = _stamp(result, environment, started_at, time.time())
        return _record_evidence(
            stamped,
            requested=operation,
            effective=effective,
            environment=environment,
            store=evidence_store,
            secrets=secrets,
            sink=evidence_sink,
            disposition=disposition,
        )

    def _failed(message: str, effective: Operation | None) -> OperationResult:
        """A failure raised by the pipeline itself, before the handler was entered.

        Every caller of this helper sits above ``spec.run``. A handler that
        crashed is a different terminal state and builds its result inline, so
        the two can never share a disposition by accident.
        """
        return _finish(
            OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=message,
                rendering=message,
            ),
            effective,
            HandlerDisposition.NOT_REACHED,
        )

    try:
        normalized = normalize(operation)
    except (UnknownOperationKind, ValueError) as exc:
        return _failed(str(exc), None)

    # From here to the handler call there is exactly ONE operation value, and it
    # is the post-rewrite one. That single-name discipline is what makes "the
    # gate saw what ran" structural instead of a thing to remember: gating the
    # pre-rewrite form is not a subtle mistake to avoid, it is unexpressible,
    # because the pre-rewrite form is no longer bound to anything below.
    try:
        effective = apply_rewrite(normalized, rewrite(normalized) if rewrite else None)
    except RewriteRejected as exc:
        return _failed(str(exc), normalized)
    except Exception as exc:  # noqa: BLE001 - a consumer's rewrite is not trusted
        return _failed(
            f"the rewrite for {normalized.kind} raised: {type(exc).__name__}: {exc}",
            normalized,
        )

    # The gate precedes the preview branch on purpose: "this is denied" is more
    # useful, and safer, than a preview implying it would otherwise have run.
    verdict = _policy_gate(effective, policy)
    if verdict.denied:
        return _finish(
            OperationResult(
                operation_id=effective.id,
                status=OperationStatus.DENIED,
                verdict=verdict,
                error=verdict.reason,
                rendering=verdict.reason,
            ),
            effective,
            HandlerDisposition.NOT_REACHED,
        )

    if effective.requires_apply and not effective.apply:
        rendering = (
            f"preview: {effective.kind} was not applied. "
            "Re-issue with apply=True to carry it out."
        )
        return _finish(
            OperationResult(
                operation_id=effective.id,
                status=OperationStatus.PREVIEWED,
                verdict=verdict,
                rendering=rendering,
                output={"kind": effective.kind, "arguments": dict(effective.arguments)},
                # No effects, and no claim of completeness: a preview describes
                # what would run, it does not predict what would change.
                effects=Effects(complete=False),
            ),
            effective,
            HandlerDisposition.NOT_REACHED,
        )

    # ``handler_for`` sits outside the try deliberately, so the CRASHED window
    # covers the handler's own body and nothing else. A lookup failure here is a
    # pipeline error, not evidence that project code ran.
    spec = handler_for(effective.kind)
    try:
        result = spec.run(effective, environment)
    except Exception as exc:  # noqa: BLE001 - a handler crash must stay recoverable
        # The handler was entered and died. It may have completed part of its
        # work — a half-written file, a process that started — and nothing at
        # this layer can distinguish that from having done nothing at all. The
        # record says "unknown" rather than picking the convenient answer.
        message = f"{effective.kind} failed: {type(exc).__name__}: {exc}"
        return _finish(
            OperationResult(
                operation_id=effective.id,
                status=OperationStatus.FAILED,
                verdict=verdict,
                error=message,
                rendering=message,
            ),
            effective,
            HandlerDisposition.CRASHED,
        )

    return _finish(replace(result, verdict=verdict), effective, HandlerDisposition.COMPLETED)
