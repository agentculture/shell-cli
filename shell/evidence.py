"""Evidence: the durable record of what an operation did — and what it could not see.

Evidence is a product surface, not a debug log. Four different readers consume
the same record: the model deciding what to do next, the operator auditing a run,
a telemetry pipeline, and an external validator that trusts none of the above.
That audience is why this module exists separately from
:class:`shell.results.Evidence`, which is the *live* evidence attached to one
result. This module turns that into a **record**: assembled from the requested
and normalized operation, redacted, versioned, hashed and persisted.

Three commitments shape the design, and each of them is a limit as much as a
feature.

**Redaction is best-effort and says so in the payload.** Declared secret values
are removed everywhere they appear. A secret nobody declared — one a command
printed on its own — is not detected, because detecting it would mean guessing
which strings are sensitive. So the record never claims to be clean: it carries
``redaction.complete``, which is :data:`REDACTION_IS_COMPLETE` and therefore
always ``False``. This follows the precedent set by
``Environment.network_enforced``: a control the code cannot actually deliver is
reported as undelivered rather than implied.

**A failed write is never a silent success.** If the record cannot be persisted,
:func:`capture` returns a result whose evidence is marked ``degraded`` with a
reason. An executed action that could not be recorded must not read as a clean
run — the caller learns that the action happened *and* that the trail is missing.

**The digest covers what is stored, not what was produced.** Output digests are
taken after redaction and after truncation. A digest of the original stream would
be an offline brute-force oracle for any short secret the record just removed, so
the record attests to its own contents and states that scope plainly.

**"Was it applied?" is answered by the dispatcher, not inferred from the status.**
Only the pipeline knows whether it reached the handler, and a status string does
not carry that: ``failed`` covers both an operation rejected before the handler
was entered and a handler that crashed halfway through a write. The first was
definitively not applied; the second is genuinely unknown. So the caller states
what happened via :class:`HandlerDisposition`, and ``applied`` is allowed to be
``None`` rather than forced into a boolean it cannot honestly fill.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from shell.results import SCHEMA_VERSION, OperationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shell.environment import Environment
    from shell.operations import Operation

__all__ = [
    "DEFAULT_STORE_SUBDIR",
    "REDACTED",
    "REDACTION_IS_COMPLETE",
    "EvidenceRecord",
    "EvidenceStore",
    "HandlerDisposition",
    "Redactor",
    "RetentionPolicy",
    "WriteOutcome",
    "build_record",
    "capture",
]

#: What a redacted secret value is replaced with. Fixed and non-empty so a reader
#: can see that something was removed rather than silently reading a gap.
REDACTED = "[redacted]"

#: Whether redaction can ever be claimed complete. It cannot, and this constant
#: exists so that fact is a value in the payload rather than a line in a document
#: someone may not have read. Nothing in this module sets it True.
REDACTION_IS_COMPLETE = False

#: Where records live relative to the trusted control root. See
#: :meth:`EvidenceStore.for_environment` for why it is anchored to the *source*
#: root and what that does not guarantee.
DEFAULT_STORE_SUBDIR = Path(".shell") / "evidence"


class HandlerDisposition(str, Enum):
    """How far dispatch got before the operation reached its terminal state.

    This exists because the result status cannot answer it. ``failed`` covers an
    operation rejected before the handler was entered *and* a handler that raised
    partway through its work, and those two have opposite answers to the only
    question an auditor really asks — did anything happen?

    The dispatcher knows which one occurred; nobody downstream can recover it.
    So it is stated rather than inferred.
    """

    #: Dispatch never called the handler. An unknown kind, a rejected rewrite, a
    #: policy denial, a preview. Nothing was applied, definitively.
    NOT_REACHED = "not_reached"

    #: The handler was called and returned. Whether it applied anything is then
    #: exactly what the caller asked for.
    COMPLETED = "completed"

    #: The handler was called and raised. It may have completed part of its work
    #: — a partial write may be on disk — and nothing at this layer can tell.
    #: ``applied`` is ``None`` here, and that is the honest answer.
    CRASHED = "crashed"

    #: The caller did not say. Only for records built outside the dispatch
    #: pipeline; ``applied`` degrades to ``None`` for any non-success status
    #: rather than guessing in the dangerous direction.
    UNSTATED = "unstated"


def _applied_state(
    result: OperationResult,
    requested: Operation,
    disposition: HandlerDisposition,
) -> tuple[bool | None, bool | None]:
    """Return ``(applied, handler_entered)`` for the record.

    ``applied`` is deliberately three-valued. Forcing it to a boolean is what
    made a denied operation read as an applied one, and the same forcing in the
    other direction would make a crashed handler claim it changed nothing — a
    false statement about a process that may have written half a file.
    """
    # A preview or a denial never reaches the handler, whatever the caller asked
    # for and whatever the dispatcher says. Checked first so the two independent
    # facts cannot disagree.
    if result.previewed or result.denied:
        return False, False

    if disposition is HandlerDisposition.NOT_REACHED:
        return False, False

    if disposition is HandlerDisposition.CRASHED:
        return None, True

    if disposition is HandlerDisposition.COMPLETED:
        return bool(requested.apply), True

    # UNSTATED: the record was built outside dispatch. A success can only have
    # come from a handler that ran; anything else is unknowable from here.
    if result.succeeded:
        return bool(requested.apply), None
    return None, None


# --- redaction --------------------------------------------------------------


@dataclass(frozen=True)
class Redactor:
    """Removes *declared* secret values from a record, and counts what it removed.

    The scope of this class is the single most important thing about it, so it is
    stated as a contract rather than left to be inferred:

    **Redacted.** Every value in ``secrets``, wherever it appears in the record —
    in captured stdout or stderr, in operation arguments, in a rendering, in an
    error message, in a policy reason. The whole record body is walked; there is
    no field a declared secret can hide in.

    **Not redacted.** Anything not declared. A command that prints a credential
    the caller never handed to this class emits it into stdout, and stdout goes
    into the record verbatim. There is no pattern library here and deliberately
    so: a heuristic that catches "things shaped like an AWS key" would create a
    false sense of coverage far more dangerous than the honest gap, because a
    reader would stop declaring secrets and trust the scanner.

    The names of declared secrets are recorded; their values never are. That is
    the same posture ``Environment.secret_names`` takes — names travel, values do
    not.
    """

    #: Secret name -> secret value. Held only for the duration of a capture; the
    #: values are never written to a record, a log, or a returned payload.
    secrets: Mapping[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        """Declared secret names, sorted. Safe to record."""
        return tuple(sorted(self.secrets))

    def _ordered_values(self) -> tuple[str, ...]:
        """Non-empty secret values, longest first.

        Longest-first matters: when one secret is a substring of another,
        replacing the shorter one first would leave the remainder of the longer
        one exposed in the record.
        """
        values = {str(v) for v in self.secrets.values() if v}
        return tuple(sorted(values, key=len, reverse=True))

    def scrub_text(self, text: str) -> tuple[str, int]:
        """Return *text* with declared secrets replaced, and the number replaced."""
        count = 0
        for value in self._ordered_values():
            occurrences = text.count(value)
            if occurrences:
                text = text.replace(value, REDACTED)
                count += occurrences
        return text, count

    def scrub(self, value: Any) -> tuple[Any, int]:
        """Recursively scrub any JSON-shaped value, returning it and a count.

        Dictionary *keys* are scrubbed as well as values. A caller that used a
        secret as a key would otherwise leak it through the structure itself.
        """
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            out: dict[Any, Any] = {}
            total = 0
            for key, item in value.items():
                new_key, key_count = self.scrub_text(key) if isinstance(key, str) else (key, 0)
                new_item, item_count = self.scrub(item)
                out[new_key] = new_item
                total += key_count + item_count
            return out, total
        if isinstance(value, (list, tuple)):
            items = []
            total = 0
            for item in value:
                new_item, item_count = self.scrub(item)
                items.append(new_item)
                total += item_count
            return items, total
        return value, 0


# --- retention --------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """How much evidence a store keeps.

    Retention is enforced **only when the store is written to** — there is no
    daemon, no timer, and no process that visits an idle store. A store that
    stops receiving records keeps whatever it last held, indefinitely. That is a
    deliberate consequence of staying pure-stdlib with no background thread, and
    it is stated here because "records older than 14 days are deleted" would
    otherwise read as a guarantee.

    Retention also governs only this store's own directory. A record a consumer
    has already read, copied, or forwarded to telemetry is beyond reach; pruning
    is housekeeping, never a deletion guarantee.

    ``None`` on either bound disables that bound.
    """

    max_records: int | None = 500
    max_age_seconds: float | None = 14 * 24 * 60 * 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_records": self.max_records,
            "max_age_seconds": self.max_age_seconds,
            "enforced_on_write_only": True,
        }


# --- the record -------------------------------------------------------------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class EvidenceRecord:
    """One persisted evidence record: a plain JSON-shaped body plus accessors.

    The body is deliberately a nested mapping rather than fifty dataclass
    fields. Every reader of this record is reading JSON — the CLI emits it, a
    validator parses it from disk, telemetry ships it — so the mapping *is* the
    contract, and a dataclass mirroring it would be a second shape to keep in
    sync. The accessors below exist for ergonomics, not as the schema.
    """

    body: Mapping[str, Any]

    @property
    def schema_version(self) -> str:
        return str(self.body["schema_version"])

    @property
    def record_id(self) -> str:
        return str(self.body["record_id"])

    @property
    def operation_id(self) -> str:
        return str(self.body["operation_id"])

    @property
    def status(self) -> str:
        return str(self.body["status"])

    @property
    def degraded(self) -> bool:
        return bool(self.body["evidence_quality"]["degraded"])

    @property
    def redaction_complete(self) -> bool:
        """Always ``False``. See :data:`REDACTION_IS_COMPLETE`."""
        return bool(self.body["redaction"]["complete"])

    @property
    def effects_complete(self) -> bool:
        return bool(self.body["effects"]["complete"])

    def to_dict(self) -> dict[str, Any]:
        """The record as JSON, with a content digest over everything else.

        ``integrity.content_sha256`` is computed over the canonical serialization
        of the rest of the body, so an external validator can recompute it with
        the recipe named in ``integrity.algorithm`` and detect tampering after
        the fact. It attests to the *record*; it says nothing about whether the
        record described the world accurately.
        """
        body = json.loads(_canonical(self.body))
        body["integrity"] = {
            "algorithm": "sha256(json(body, sort_keys=True, separators=(',', ':')))",
            "content_sha256": _digest(_canonical(body)),
        }
        return body

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)


def _output_capture(
    text: str,
    *,
    truncated: bool,
    original_bytes: int | None,
) -> dict[str, Any]:
    """One captured stream, with its bounds and digest described honestly."""
    return {
        "text": text,
        "truncated": truncated,
        # Byte count of the *original* stream when the producer knew it. None
        # means nobody measured it — distinct from zero.
        "original_bytes": original_bytes,
        "stored_bytes": len(text.encode("utf-8", errors="replace")),
        # Digest of the text as stored: after redaction and after truncation.
        # Not a digest of the original stream, deliberately — see the module
        # docstring on why that would be a brute-force oracle.
        "sha256": _digest(text),
        "sha256_scope": "text-as-stored",
    }


def _caller_block(caller: Mapping[str, str]) -> dict[str, Any]:
    """Provenance, with the three fields every consumer asks for promoted."""
    return {
        "agent": caller.get("agent"),
        "task_id": caller.get("task_id"),
        "tool": caller.get("tool"),
        # The caller's full provenance map, kept whole: shell-cli does not own
        # these semantics and must not drop keys it does not recognize.
        "all": dict(caller),
    }


def build_record(
    result: OperationResult,
    *,
    requested: Operation,
    normalized: Operation | None = None,
    environment: Environment | None = None,
    redactor: Redactor | None = None,
    recorded_at: float | None = None,
    disposition: HandlerDisposition = HandlerDisposition.UNSTATED,
) -> EvidenceRecord:
    """Assemble the evidence record for one completed operation.

    Both the *requested* and the *normalized* operation are recorded. They differ
    whenever normalization resolved an intent or a profile, and a reader auditing
    a decision needs to see what was asked for as well as what was run — a record
    holding only the normalized form cannot answer "what did the caller actually
    request?".

    ``normalized`` may be ``None`` when normalization never happened, which is
    exactly the case for an operation that failed on an unknown kind. That is
    recorded as ``operation.normalized_available = false`` rather than by
    silently copying the requested form into both slots.

    ``disposition`` is how far dispatch got. It defaults to
    :attr:`HandlerDisposition.UNSTATED` for callers building a record outside the
    pipeline, which costs them a three-valued ``applied`` on any non-success
    status — the safe degradation, since the alternative is asserting something
    this function has no way to know.
    """
    redactor = redactor or Redactor()
    evidence = result.evidence
    applied, handler_entered = _applied_state(result, requested, disposition)

    resources: dict[str, Any] = {
        "timeout_seconds": requested.timeout_seconds,
        "max_output_bytes": requested.max_output_bytes,
        "resolved_from_environment": False,
    }
    if environment is not None:
        resources = {
            "timeout_seconds": requested.resolved_timeout(environment),
            "max_output_bytes": requested.resolved_max_output_bytes(environment),
            "resolved_from_environment": True,
        }

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": uuid.uuid4().hex,
        "recorded_at": time.time() if recorded_at is None else recorded_at,
        "operation_id": result.operation_id,
        "status": result.status.value,
        "caller": _caller_block(requested.caller),
        "operation": {
            "kind": requested.kind,
            "requested": requested.to_dict(),
            "normalized": None if normalized is None else normalized.to_dict(),
            "normalized_available": normalized is not None,
        },
        "execution": {
            # Preview and applied are recorded as independent facts. A preview is
            # not a flavour of success, and a reader must not have to derive "did
            # this happen?" from the status string alone.
            #
            # ``applied`` is THREE-valued: true, false, or null for "the handler
            # was entered and crashed, so nobody can say". Deriving it from
            # ``requested.apply`` alone reported every denied operation — and
            # every operation rejected before the handler — as applied, which is
            # the opposite of what happened. Collapsing the crash case to false
            # would be the same error mirrored: a handler that died mid-write did
            # not necessarily change nothing.
            "applied": applied,
            # The underlying fact ``applied`` is derived from, recorded on its
            # own so an auditor never has to reverse-engineer it. Null only when
            # the record was built outside the dispatch pipeline.
            "handler_entered": handler_entered,
            "handler_disposition": disposition.value,
            "previewed": result.previewed,
            "requested_apply": bool(requested.apply),
            "exit_code": evidence.exit_code,
            "error": result.error,
            "started_at": evidence.started_at,
            "ended_at": evidence.ended_at,
            "duration_ms": evidence.duration_ms,
            "resources": resources,
        },
        "policy": result.verdict.to_dict(),
        "environment": {
            "id": evidence.environment_id,
            "workspace_kind": evidence.workspace_kind,
            "runner": evidence.backend,
            "isolation": evidence.isolation,
            "isolation_note": evidence.isolation_note,
            "root": evidence.root,
            "cwd": evidence.cwd,
            "mounts": list(evidence.mounts),
            "network": evidence.network,
            # A declared network policy the runner cannot apply is a record of
            # intent, not a control. Carried through verbatim from the result.
            "network_enforced": evidence.network_enforced,
        },
        "output": {
            # Captured separately, and kept that way. The first consumer renders
            # them concatenated; that concatenation belongs in its adapter, not
            # here, because a record that has thrown the distinction away cannot
            # get it back.
            "stdout": _output_capture(
                evidence.stdout,
                truncated=evidence.stdout_truncated,
                original_bytes=evidence.stdout_bytes,
            ),
            "stderr": _output_capture(
                evidence.stderr,
                truncated=evidence.stderr_truncated,
                original_bytes=evidence.stderr_bytes,
            ),
            "rendering": result.rendering,
            "structured": dict(result.output),
        },
        "effects": result.effects.to_dict(),
        "evidence_quality": {
            "degraded": evidence.degraded,
            "degraded_reason": evidence.degraded_reason,
        },
    }

    scrubbed, count = redactor.scrub(body)
    scrubbed["redaction"] = {
        # Names only. Values are never recorded, in any field, at any time.
        "secret_names": list(redactor.names),
        "replacements": count,
        "placeholder": REDACTED,
        "complete": REDACTION_IS_COMPLETE,
        "scope": (
            "Declared secret values are removed from the whole record body. "
            "Secrets that were not declared are not detected and are recorded "
            "verbatim if a command emitted them."
        ),
    }
    return EvidenceRecord(body=scrubbed)


# --- storage ----------------------------------------------------------------


@dataclass(frozen=True)
class WriteOutcome:
    """What happened when a record was offered to a store."""

    ok: bool
    path: Path | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": None if self.path is None else str(self.path),
            "error": self.error,
        }


@dataclass(frozen=True)
class EvidenceStore:
    """A directory of evidence records, one JSON file per operation.

    One file per record rather than an appended log: pruning by age becomes a
    file listing instead of a rewrite, and a write that fails partway corrupts
    nothing that was already recorded.

    Filenames sort chronologically (``<recorded_at>-<record_id>.json``) so a
    reader can page through them in order without opening any.
    """

    directory: Path
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)

    @classmethod
    def for_environment(
        cls,
        environment: Environment,
        *,
        retention: RetentionPolicy | None = None,
    ) -> EvidenceStore:
        """Anchor a store under the environment's **source** root.

        The source root is trusted control context; the work root is what
        model-driven operations may change. Evidence is anchored to the former so
        that an agent rewriting files in its workspace is not also rewriting the
        record of what it did.

        That separation is real only when the deployment provides it. When
        ``source_root`` and ``work_root`` are the same directory — which is the
        common interactive case — the store sits inside the writable tree and
        this ordering buys nothing. :func:`capture` records which case applied,
        per record, as ``integrity.store_outside_work_root``.
        """
        return cls(
            directory=Path(environment.source_root) / DEFAULT_STORE_SUBDIR,
            retention=retention or RetentionPolicy(),
        )

    def _filename(self, record: EvidenceRecord) -> str:
        return f"{float(record.body['recorded_at']):.6f}-{record.record_id}.json"

    def write(self, record: EvidenceRecord) -> WriteOutcome:
        """Persist *record* atomically. Never raises — failure is a return value.

        Written to a temporary file in the destination directory and moved into
        place, so a reader never observes a half-written record. Failure comes
        back as ``WriteOutcome(ok=False)`` because the caller of this module is
        recording something that already happened: raising here would replace a
        real outcome with an exception about the paperwork.
        """
        target = self.directory / self._filename(record)
        handle = None
        temp_name = ""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                dir=str(self.directory), prefix=".partial-", suffix=".json"
            )
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            handle.write(record.to_json())
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temp_name, target)
        except (OSError, ValueError, TypeError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - close-after-failure
                    pass
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            return WriteOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

        self.prune()
        return WriteOutcome(ok=True, path=target)

    def paths(self) -> list[Path]:
        """Record files, oldest first. An unreadable or absent store is empty.

        The in-flight temporary files :meth:`write` creates are named with a
        leading dot, and ``*`` in a glob does not match a leading dot — so a
        half-written record can never be listed or read as a finished one.
        """
        try:
            return sorted(p for p in self.directory.glob("*.json") if p.is_file())
        except OSError:
            return []

    def records(self) -> list[dict[str, Any]]:
        """Every readable record, oldest first.

        A record that will not parse is skipped rather than raising: one corrupt
        file must not make the whole audit trail unreadable.
        """
        out: list[dict[str, Any]] = []
        for path in self.paths():
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def prune(self, *, now: float | None = None) -> list[Path]:
        """Apply the retention policy. Returns the paths actually removed.

        Best-effort by construction: a file that cannot be removed is left in
        place and skipped. Pruning is housekeeping, and a failure to tidy must
        never propagate into the outcome of the operation being recorded.
        """
        now = time.time() if now is None else now
        paths = self.paths()
        doomed: list[Path] = []

        if self.retention.max_age_seconds is not None:
            cutoff = now - self.retention.max_age_seconds
            for path in paths:
                try:
                    if path.stat().st_mtime < cutoff:
                        doomed.append(path)
                except OSError:
                    continue

        if self.retention.max_records is not None:
            survivors = [p for p in paths if p not in doomed]
            excess = len(survivors) - self.retention.max_records
            if excess > 0:
                doomed.extend(survivors[:excess])

        removed: list[Path] = []
        for path in doomed:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
        return removed


# --- the entry point --------------------------------------------------------


def capture(
    result: OperationResult,
    *,
    requested: Operation,
    normalized: Operation | None = None,
    environment: Environment | None = None,
    store: EvidenceStore | None = None,
    secrets: Mapping[str, str] | None = None,
    disposition: HandlerDisposition = HandlerDisposition.UNSTATED,
) -> tuple[OperationResult, EvidenceRecord]:
    """Build, redact and persist the record for *result*; report honestly on failure.

    Returns the result to hand onward and the record that was built. When no
    ``store`` is given the record is built and returned but not persisted, and
    that is **not** degraded: the record reached its caller, which is a delivery
    channel. Persistence is recorded as ``integrity.persisted``.

    When a store *is* given and the write fails, the returned result carries
    ``evidence.degraded = True`` and a reason naming the failure. This is the
    whole point of the function. An operation that ran and could not be recorded
    is a worse position than one that failed, because nothing downstream can tell
    the difference unless the result says so.

    The record itself cannot report its own failure to be written — it is not on
    disk to be read. That asymmetry is why the marker lives on the result.
    """
    redactor = Redactor(secrets=dict(secrets or {}))
    record = build_record(
        result,
        requested=requested,
        normalized=normalized,
        environment=environment,
        redactor=redactor,
        disposition=disposition,
    )

    outside_work_root: bool | None = None
    if store is not None and environment is not None:
        outside_work_root = not _is_within(store.directory, Path(environment.work_root))

    def _with_persistence(outcome: WriteOutcome, reason: str) -> EvidenceRecord:
        """Return a new record carrying its own persistence block.

        A new record rather than a mutation: :class:`EvidenceRecord` is frozen,
        and a caller holding the pre-write record must not see it change under
        them because a write happened afterwards.
        """
        return EvidenceRecord(
            body={
                **record.body,
                "persistence": {
                    "persisted": outcome.ok,
                    "path": None if outcome.path is None else str(outcome.path),
                    "reason": reason,
                    # Whether the record landed outside the tree the operation
                    # itself could write to. None when that is not determinable.
                    "store_outside_work_root": outside_work_root,
                },
            }
        )

    if store is None:
        return result, _with_persistence(WriteOutcome(ok=False), "no evidence store was configured")

    outcome = store.write(record)
    persisted = _with_persistence(outcome, outcome.error)
    if outcome.ok:
        return result, persisted

    reason, _ = redactor.scrub_text(
        f"evidence record {record.record_id} could not be persisted to "
        f"{store.directory}: {outcome.error}"
    )
    degraded_evidence = _replace(
        result.evidence,
        degraded=True,
        degraded_reason=(
            f"{result.evidence.degraded_reason}; {reason}"
            if result.evidence.degraded_reason
            else reason
        ),
    )
    return _replace(result, evidence=degraded_evidence), persisted


def _is_within(path: Path, root: Path) -> bool:
    """Whether *path* is *root* or sits underneath it, compared as resolved paths."""
    try:
        resolved = Path(path).expanduser().resolve()
        resolved_root = Path(root).expanduser().resolve()
    except OSError:  # pragma: no cover - resolution failure on an exotic path
        return False
    return resolved == resolved_root or resolved_root in resolved.parents
