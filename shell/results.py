"""Neutral operation results: status, policy verdict, effects, evidence.

Everything here is provider-neutral and JSON-serializable. No consumer type
appears in any signature — colleague maps these shapes into its own
``ToolOutcome`` / ``Step`` / media-message contracts on its side of the seam,
and shell-cli knows nothing about them.

Three commitments shape this module:

* **A preview is never reported as success.** ``PREVIEWED`` is its own status,
  and every success predicate exposed here (:attr:`OperationResult.succeeded`
  and ``__bool__``) is false for it. A preview describes what *would* happen; it
  does not predict effects.
* **Effects carry an honest completeness marker.** :attr:`Effects.complete`
  defaults to ``False``: an effect list is a claim about what was observed, and
  the default claim is "this may be partial". Only a handler that genuinely
  enumerated every effect may set it.
* **Evidence records isolation honestly.** :attr:`Evidence.isolation` is the
  runner's own self-description. For host execution it is ``"none"`` — the guard
  is best-effort and a process it starts can reach the whole machine.
* **A declared secret is removed from the live result, not only the record.**
  :attr:`Evidence.secret_handling` says which of the three things happened, and
  :attr:`Evidence.redaction_complete` is permanently ``False`` — see
  :data:`REDACTION_IS_COMPLETE`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "REDACTION_IS_COMPLETE",
    "SCHEMA_VERSION",
    "Effects",
    "Evidence",
    "OperationResult",
    "OperationStatus",
    "PolicyDecision",
    "PolicyVerdict",
    "SecretHandling",
]

#: Whether redaction can ever be claimed complete. It cannot.
#:
#: Declared secrets are removed wherever they appear; a secret nobody declared is
#: not detected, because detecting it would mean guessing which strings are
#: sensitive. So no surface in this package — record or live result — ever claims
#: to be clean. This constant is the single source of that answer;
#: :mod:`shell.evidence` re-exports it rather than keeping a second copy that
#: could drift.
REDACTION_IS_COMPLETE = False

# Version of the whole neutral cross-repo contract — ``Operation``,
# ``OperationResult`` and ``Environment`` version together, because a consumer
# that can read one must be able to read all three. It is stamped onto every
# instance so a consumer pinning an older shell-cli can detect skew from the
# payload alone, without a flag day.
#
# "0" marks the pre-Milestone-1 generation: the shapes are not yet stable.
# Compare it for exact equality and treat anything unrecognized as incompatible.
SCHEMA_VERSION = "0"


class OperationStatus(str, Enum):
    """Terminal state of an operation.

    ``PREVIEWED`` is deliberately a peer of the others rather than a flavour of
    success: a caller that treats "not failed" as "done" would otherwise report
    an unapplied mutation as completed work.
    """

    PREVIEWED = "previewed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class PolicyDecision(str, Enum):
    """Outcome of the operation-policy gate.

    ``UNGATED`` is distinct from ``ALLOWED``: an absent policy section means the
    operator declared no gate for this operation, which is not the same as a
    configured gate that permitted it. Collapsing the two would let a malformed
    or empty policy read as a deliberate allow.
    """

    UNGATED = "ungated"
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True)
class PolicyVerdict:
    """What the policy gate decided, and which rule decided it."""

    decision: PolicyDecision = PolicyDecision.UNGATED
    reason: str = ""
    matched_rule: str | None = None

    @property
    def denied(self) -> bool:
        return self.decision is PolicyDecision.DENIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


class SecretHandling(str, Enum):
    """What happened to declared secret values in *this* result.

    Three states rather than a boolean, because "not redacted" has two entirely
    different causes and an auditor must not have to guess which one applied.
    ``NONE_DECLARED`` means the caller handed over no secrets and there was
    nothing to remove; ``REVEALED`` means secrets *were* declared and the caller
    explicitly asked to see them anyway. Collapsing those two would make a
    deliberate opt-in indistinguishable from an ordinary run.

    None of the three is a claim of cleanliness. Even ``REDACTED`` leaves
    :attr:`Evidence.redaction_complete` ``False``: a command that printed a
    credential nobody declared put it in this result verbatim.
    """

    #: No secrets were passed to :func:`shell.operations.execute`.
    NONE_DECLARED = "none_declared"

    #: Declared secret values were removed from every string in this result.
    REDACTED = "redacted"

    #: Declared secrets were left in place because the caller opted in. The
    #: persisted evidence record is redacted regardless — this state describes
    #: the live result only.
    REVEALED = "revealed"


@dataclass(frozen=True)
class Effects:
    """What an operation is known to have changed.

    ``complete`` is the honest completeness marker. It defaults to ``False``
    because most effect lists cannot be fully observed: a host process may write
    anywhere it can reach, and no amount of inspection at this layer will
    enumerate that. A handler sets ``complete=True`` only when it truly performed
    every mutation itself and can name all of them.
    """

    changed_paths: tuple[str, ...] = ()
    bytes_written: int = 0
    git_refs: tuple[str, ...] = ()
    created_resources: tuple[str, ...] = ()
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": list(self.changed_paths),
            "bytes_written": self.bytes_written,
            "git_refs": list(self.git_refs),
            "created_resources": list(self.created_resources),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class Evidence:
    """The structured record of how an operation actually ran.

    Evidence is a product surface, not a debug log: the model, the operator,
    telemetry and an external validator all read it. Two rules follow.

    Secrets are never recorded — only the *names* of secrets an environment was
    willing to inject, never their values.

    Evidence failure must not turn an executed action into an unrecorded
    success. When capture degrades, ``degraded`` is set and ``degraded_reason``
    says what was lost, rather than the result quietly claiming a clean run.

    ``stdout`` and ``stderr`` are captured **separately**. A consumer that needs
    them interleaved concatenates them itself; the neutral record does not throw
    away the distinction to save the consumer a line.

    :attr:`secret_handling` and :attr:`secret_replacements` describe what was
    removed from *this result*, which is a different question from what was
    removed from the persisted record. The two can differ in exactly one
    direction: a caller may opt into seeing declared secrets live, and the record
    is redacted anyway. The reverse is not offered.
    """

    backend: str = "unknown"
    isolation: str = "unknown"
    isolation_note: str = ""
    environment_id: str = ""
    workspace_kind: str = ""
    root: str | None = None
    cwd: str | None = None
    network: str = ""
    #: Whether the runner can actually enforce the declared network policy. A
    #: declared "deny" that nothing enforces is a wish, and evidence must not
    #: let it read as a control.
    network_enforced: bool = False
    mounts: tuple[str, ...] = ()
    started_at: float | None = None
    ended_at: float | None = None
    duration_ms: float | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    degraded: bool = False
    degraded_reason: str = ""
    #: What happened to declared secrets in this result. Stamped by
    #: :func:`shell.operations.execute`; a handler never sets it, because a
    #: handler is never given the secret values in the first place.
    secret_handling: SecretHandling = SecretHandling.NONE_DECLARED
    #: How many declared secret occurrences were replaced in this result. Zero
    #: under :attr:`SecretHandling.REVEALED` because nothing was replaced, and
    #: zero under ``REDACTED`` when no declared secret happened to appear.
    secret_replacements: int = 0
    #: Permanently ``False`` — mirrors :data:`REDACTION_IS_COMPLETE`. Redacting
    #: every declared secret still leaves undeclared ones untouched, so this
    #: field never becomes a clean bill of health.
    redaction_complete: bool = REDACTION_IS_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "isolation": self.isolation,
            "isolation_note": self.isolation_note,
            "environment_id": self.environment_id,
            "workspace_kind": self.workspace_kind,
            "root": self.root,
            "cwd": self.cwd,
            "network": self.network,
            "network_enforced": self.network_enforced,
            "mounts": list(self.mounts),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "secret_handling": self.secret_handling.value,
            "secret_replacements": self.secret_replacements,
            "redaction_complete": self.redaction_complete,
        }


@dataclass(frozen=True)
class OperationResult:
    """The neutral outcome of one operation.

    ``output`` is the structured payload (whatever the handler's kind defines);
    ``rendering`` is the bounded human/model-readable form of it. They are
    separate so a consumer is never forced to parse prose to recover structure.
    """

    operation_id: str
    status: OperationStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    rendering: str = ""
    verdict: PolicyVerdict = field(default_factory=PolicyVerdict)
    effects: Effects = field(default_factory=Effects)
    evidence: Evidence = field(default_factory=Evidence)
    error: str = ""
    schema_version: str = SCHEMA_VERSION

    @property
    def succeeded(self) -> bool:
        """True only for :attr:`OperationStatus.SUCCEEDED`.

        A previewed or denied operation is not a success by this predicate or by
        ``bool(result)``.
        """
        return self.status is OperationStatus.SUCCEEDED

    @property
    def previewed(self) -> bool:
        return self.status is OperationStatus.PREVIEWED

    @property
    def denied(self) -> bool:
        return self.status is OperationStatus.DENIED

    @property
    def effects_complete(self) -> bool:
        """Whether the effect list is known to be exhaustive."""
        return self.effects.complete

    def __bool__(self) -> bool:
        """``if result:`` means "succeeded", never "did not crash".

        Defined explicitly because a dataclass is otherwise always truthy, which
        would make the natural-looking ``if result:`` treat a previewed or denied
        operation as done work.
        """
        return self.succeeded

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "status": self.status.value,
            "output": dict(self.output),
            "rendering": self.rendering,
            "verdict": self.verdict.to_dict(),
            "effects": self.effects.to_dict(),
            "evidence": self.evidence.to_dict(),
            "error": self.error,
        }
