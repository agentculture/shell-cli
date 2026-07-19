"""Operation policy: an operator-declared approval gate, evaluated over a snapshot.

This is layer 2 of the four-layer safety model — the layer that answers *may this
normalized operation proceed?* under an operator-supplied policy. It decides
nothing about capability (the caller's roles own that), nothing about execution
isolation (the runner owns that), and nothing about whether the resulting work is
acceptable (the caller's validation gates own that).

Two questions are answered, and the split is inherited from the first consumer
rather than invented here:

* may this shell command run? (:meth:`Policy.check_run_command`)
* is this hook / command file approved and unchanged? (:meth:`Policy.check_file`)

A third, :func:`check_write`, generalises a hard-coded read-only subtree into
:attr:`~shell.environment.Environment.read_only_paths`.

Config shape (``approvals.json``)::

    {
      "run_command": { "allow": ["git", "pytest", "uv"], "deny": [] },
      "hooks":    { "lint.sh":  "sha256:<hex>" },
      "commands": { "fix-lint": "sha256:<hex>" }
    }

**Enforcement is allow-list per category, and only when the section is PRESENT.**
Presence, not emptiness, is the semantic. An absent section is a strict no-op —
a repository with no policy file behaves exactly as it did before a gate existed
— while a section that exists but lists nothing is *present*, active, and simply
matches nothing.

Where the policy comes from
---------------------------

**This module resolves no configuration layout.** It takes pre-resolved candidate
paths, or a policy mapping outright, and nothing else: there is no search order,
no user-versus-repo precedence, no per-model directory construction here. Those
are the calling harness's semantics and they stay with it. Candidates are ordered
by increasing precedence and merged **whole-section**, never deep-merged, so a
later candidate that redefines ``run_command`` replaces it entirely.

:func:`snapshot` anchors candidates at
:attr:`~shell.environment.Environment.source_root` — trusted control context —
and refuses any candidate that resolves outside it or lands inside the work root.
That refusal is the point: an operation must not be able to edit its own active
authorization by writing a file into the tree it is allowed to change.

Three states, kept distinct
---------------------------

Collapsing these is how a gate quietly stops gating, so each is separately
observable:

* **absent** — nothing was declared. The category is ungated
  (:attr:`~shell.results.PolicyDecision.UNGATED`, which is deliberately not
  :attr:`~shell.results.PolicyDecision.ALLOWED`).
* **malformed / unreadable** — a source exists but could not be parsed. It
  degrades to no sections and **never raises**, because a bad policy file must
  not abort a run — but :attr:`Policy.degraded` is set and every verdict the
  policy issues names the problem. The decision matches the absent case; the
  *silence* does not.
* **expected but unresolved** — a candidate the caller marked
  :attr:`PolicyCandidate.required` did not exist. :attr:`Policy.unresolved` is
  set and, again, every verdict says so.

Deciding what to do about an untrustworthy policy belongs to the caller, not
here: :attr:`Policy.trustworthy` is exposed precisely so a consumer that wants to
fail closed can, without this module unilaterally turning a missing file into a
run-ending denial.

Only the standard library is used, consistent with the zero-base-dependency
constraint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from shell.results import SCHEMA_VERSION, PolicyDecision, PolicyVerdict

__all__ = [
    "DEFAULT_ALGO",
    "FILE_CATEGORIES",
    "POLICY_FILENAME",
    "SUPPORTED_CHECKSUM_ALGOS",
    "Policy",
    "PolicyCandidate",
    "PolicySource",
    "PolicySourceError",
    "SourceStatus",
    "check_write",
    "file_checksum",
    "load_policy",
    "snapshot",
    "verify_checksum",
]

#: The conventional filename. Nothing here searches for it — it is a default
#: argument for callers that share the first consumer's convention, and any other
#: name works equally well.
POLICY_FILENAME = "approvals.json"

#: Default checksum algorithm when an approval string does not name one.
DEFAULT_ALGO = "sha256"

#: Algorithms this module computes and verifies, in a stable order so a CLI can
#: list them deterministically in a validation error.
SUPPORTED_CHECKSUM_ALGOS = ("sha256", "md5")

_SUPPORTED_ALGOS = frozenset(SUPPORTED_CHECKSUM_ALGOS)

#: The file categories :meth:`Policy.check_file` gates. Keyed by the caller's own
#: convention — the first consumer keys ``hooks`` by repo-relative path and
#: ``commands`` by stem, and this module preserves whatever key it is handed.
FILE_CATEGORIES = frozenset({"hooks", "commands"})


class PolicySourceError(ValueError):
    """A candidate policy source is not a legitimate place to read policy from.

    Raised by :func:`snapshot` for a candidate that escapes the source root or
    resolves into the work root. This is the *authoring* side of the gate — a
    calling harness passing an unsafe candidate is a bug in the caller, and
    degrading it to an ungated no-op would turn that bug into a silent loss of
    the gate. Evaluation itself still never raises.
    """


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def file_checksum(path: str | Path, algo: str = DEFAULT_ALGO) -> str:
    """Return ``"{algo}:{hexdigest}"`` for *path*'s bytes under *algo*.

    The digest is computed over the file's raw bytes, so it is content-exact
    (line-ending and encoding sensitive). The result is the algorithm-prefixed
    form an operator records in the policy file.

    Raises :class:`OSError` when the file cannot be read and :class:`ValueError`
    for an unsupported *algo*. This is the authoring side — an operator
    generating an approval — so surfacing the error is right; the verifying side
    (:func:`verify_checksum`) is the one that must never raise.
    """
    if algo not in _SUPPORTED_ALGOS:
        raise ValueError(f"unsupported checksum algorithm: {algo!r}")
    data = Path(path).read_bytes()
    digest = hashlib.new(algo, data).hexdigest()
    return f"{algo}:{digest}"


def verify_checksum(path: str | Path, approval: str) -> bool:
    """Return ``True`` iff *path*'s bytes match the *approval* checksum.

    *approval* is an algorithm-prefixed string such as ``"sha256:<hex>"``. The
    algorithm is parsed from the prefix, the file's current digest is recomputed
    under it, and the two are compared with :func:`hmac.compare_digest` —
    constant-time, defensively, even though a checksum is not a secret.

    This is the **verifying** side of the gate and must never raise: an unknown
    algorithm, a malformed approval with no ``algo:hex`` split, or a missing or
    unreadable file all return ``False``. Cannot verify means withhold approval,
    which is the only safe failure direction for an approval gate.
    """
    if not isinstance(approval, str) or ":" not in approval:
        return False
    algo, _, expected = approval.partition(":")
    if algo not in _SUPPORTED_ALGOS or not expected:
        return False
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    actual = hashlib.new(algo, data).hexdigest()
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Sources: where a policy came from, and whether it got there intact
# ---------------------------------------------------------------------------


class SourceStatus(str, Enum):
    """What happened when a candidate policy source was read.

    ``ABSENT`` and ``MALFORMED`` both contribute no sections, and that is the
    whole reason they are separate values: they are indistinguishable by their
    effect on a verdict and must remain distinguishable in the record.
    """

    #: The path does not exist. Nothing was declared here.
    ABSENT = "absent"
    #: Parsed into a section mapping.
    LOADED = "loaded"
    #: Exists, but is not valid JSON or is not a JSON object.
    MALFORMED = "malformed"
    #: Exists, but could not be read.
    UNREADABLE = "unreadable"
    #: Supplied inline by the caller rather than read from disk.
    INLINE = "inline"


@dataclass(frozen=True)
class PolicyCandidate:
    """One pre-resolved place a policy may live.

    ``required`` records the caller's *expectation*. A required candidate that
    turns out to be absent makes the resulting policy
    :attr:`~Policy.unresolved` — the difference between "no policy was declared"
    and "a declared policy did not arrive".
    """

    path: Path
    required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True)
class PolicySource:
    """The record of one candidate: where it was, and what came back."""

    status: SourceStatus
    path: Path | None = None
    sections: Mapping[str, Any] = None  # type: ignore[assignment]
    required: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.sections is None:
            object.__setattr__(self, "sections", {})

    @property
    def broken(self) -> bool:
        """Whether this source exists but could not be turned into a policy."""
        return self.status in (SourceStatus.MALFORMED, SourceStatus.UNREADABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "path": None if self.path is None else str(self.path),
            "sections": sorted(self.sections),
            "required": self.required,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Coercion helpers: tolerate bad shapes, never raise, never gate by accident
# ---------------------------------------------------------------------------


def _str_list(value: object) -> list[str]:
    """Coerce a config value to a list of strings, tolerating bad shapes.

    A non-list, or a list with non-string members, degrades gracefully: only
    string members survive. This keeps a malformed allow/deny list from gating
    unexpectedly or raising.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _str_map(value: object) -> dict[str, str]:
    """Coerce a section value to a ``{name: approval}`` map of strings."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _first_token(command: str) -> str | None:
    """Return the first shlex token of *command*, or ``None`` if there is none.

    An empty or whitespace-only command yields ``None``. A command that does not
    lex cleanly — an unbalanced quote, say — is treated as having no parseable
    token rather than raising. Under an allow-list that denies, which is the safe
    direction for a gate.
    """
    if not command or not command.strip():
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    return parts[0] if parts else None


def _sections_from_object(raw: Mapping[str, Any]) -> dict[str, dict]:
    """Reduce a parsed policy document to its well-shaped sections.

    Only sections whose value is itself an object survive. A section with the
    wrong shape — a string, a list, ``null`` — is dropped, so it reads as absent
    rather than gating unexpectedly.

    *raw* is already known to be a mapping: callers separate "this document is
    not an object at all" from "this section is not an object", because the first
    is a malformed source and the second is a dropped section.
    """
    return {key: value for key, value in raw.items() if isinstance(value, dict)}


def _read_source(candidate: PolicyCandidate) -> PolicySource:
    """Read one candidate into a :class:`PolicySource`. Never raises.

    Every failure mode gets its own status rather than collapsing into one
    "empty" outcome, because a policy that is absent and a policy that is
    corrupt are different facts about the operator's intent.
    """
    path = candidate.path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PolicySource(
            status=SourceStatus.ABSENT,
            path=path,
            required=candidate.required,
            detail="no policy file at this path",
        )
    except OSError as exc:
        return PolicySource(
            status=SourceStatus.UNREADABLE,
            path=path,
            required=candidate.required,
            detail=f"could not be read: {type(exc).__name__}",
        )

    try:
        raw = json.loads(text)
    except ValueError as exc:
        return PolicySource(
            status=SourceStatus.MALFORMED,
            path=path,
            required=candidate.required,
            detail=f"is not valid JSON: {type(exc).__name__}",
        )

    if not isinstance(raw, dict):
        return PolicySource(
            status=SourceStatus.MALFORMED,
            path=path,
            required=candidate.required,
            detail=f"is not a JSON object (found {type(raw).__name__})",
        )

    return PolicySource(
        status=SourceStatus.LOADED,
        path=path,
        sections=_sections_from_object(raw),
        required=candidate.required,
    )


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


class Policy:
    """A resolved policy snapshot: present sections gate, absent ones no-op.

    Construct via :func:`load_policy` or :func:`snapshot`. This constructor takes
    the already-merged section mappings plus the set of section names that were
    *present* in at least one contributing source. Presence — not emptiness — is
    what drives gating: a ``run_command`` section that exists but lists nothing is
    still present, and simply matches nothing; a *missing* ``run_command`` section
    is a strict no-op.

    ``Policy()`` with no arguments is the empty policy. It is a total no-op and,
    apart from an empty source list, is indistinguishable from a policy loaded
    from a candidate set that declared nothing — same section presence, same
    verdict decisions, same :meth:`to_dict` key set.
    """

    def __init__(
        self,
        *,
        run_command: dict | None = None,
        files: dict[str, dict] | None = None,
        present: frozenset[str] | None = None,
        sources: Sequence[PolicySource] = (),
        source_root: Path | None = None,
        roots_are_separate: bool = True,
    ) -> None:
        self._run_command = run_command or {}
        # Per-category name -> approval-string maps, e.g. {"hooks": {...}}.
        self._files = files or {}
        # Section names present in at least one contributing source.
        self._present = present or frozenset()
        self._sources = tuple(sources)
        self._source_root = source_root
        self._roots_are_separate = roots_are_separate

    # -- shape ------------------------------------------------------------

    def is_empty(self) -> bool:
        """Whether **no** sections are present at all.

        An empty policy is a total no-op: every check passes through ungated.
        """
        return not self._present

    def section_present(self, category: str) -> bool:
        """Whether *category* was present in any source — that is, whether it gates."""
        return category in self._present

    def file_approval(self, category: str, name: str) -> str | None:
        """The recorded approval string for ``(category, name)`` in the merged policy.

        ``None`` when the category is absent or has no entry for *name*. A
        read-only view for introspection, so a listing reflects the same merge
        enforcement uses rather than a raw single-file read.
        """
        return self._files.get(category, {}).get(name)

    def run_command_config(self) -> dict | None:
        """The merged ``run_command`` allow/deny config, or ``None`` when absent."""
        if "run_command" not in self._present:
            return None
        return self._run_command

    @property
    def sources(self) -> tuple[PolicySource, ...]:
        """Every candidate considered, in increasing precedence order."""
        return self._sources

    @property
    def source_root(self) -> Path | None:
        """The trusted root candidates were anchored at, when :func:`snapshot` was used."""
        return self._source_root

    @property
    def roots_are_separate(self) -> bool:
        """Whether the snapshot came from a tree distinct from the work root.

        ``False`` means the deployment pointed both roots at one directory, so
        the separation this snapshot relies on does not exist there. That is a
        property of the deployment, recorded rather than corrected.
        """
        return self._roots_are_separate

    # -- trust ------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """Whether any source existed but could not be turned into policy."""
        return any(source.broken for source in self._sources)

    @property
    def unresolved(self) -> bool:
        """Whether a source the caller marked required turned out to be absent."""
        return any(
            source.required and source.status is SourceStatus.ABSENT for source in self._sources
        )

    @property
    def trustworthy(self) -> bool:
        """Whether every candidate resolved exactly as the caller expected.

        A caller that wants to fail closed on an untrustworthy policy checks this
        and refuses. This module does not refuse on its behalf: turning a
        corrupt file into a run-ending denial is a decision with consequences the
        caller owns, and making it here would hide it.
        """
        return not (self.degraded or self.unresolved)

    @property
    def trust_note(self) -> str:
        """A human- and model-readable account of every source that misbehaved.

        Empty when the policy is trustworthy. Appended to the reason of every
        verdict this policy issues, so a degraded gate cannot pass unremarked
        even for a caller that never inspects :attr:`trustworthy`.
        """
        notes = []
        for source in self._sources:
            if source.broken:
                notes.append(f"policy source {source.path} {source.detail}")
            elif source.required and source.status is SourceStatus.ABSENT:
                notes.append(f"required policy source {source.path} is missing")
        return "; ".join(notes)

    def _annotate(self, reason: str) -> str:
        note = self.trust_note
        if not note:
            return reason
        prefix = f"{reason} " if reason else ""
        return f"{prefix}[policy degraded: {note}]"

    # -- checks -----------------------------------------------------------

    def check_run_command(self, command: str) -> PolicyVerdict:
        """Gate a shell command by its program token.

        Semantics:

        * If the ``run_command`` section is **absent**, the verdict is
          ``UNGATED`` — the category is not gated at all, which is distinct from
          a configured gate that permitted the command.
        * Otherwise the program token is ``shlex.split(command)[0]``; an empty or
          unparseable command yields no token. Then:

          - a token on ``deny`` is denied (deny wins over allow);
          - a token absent from a **present and non-empty** ``allow`` list is
            denied;
          - anything else is allowed.

        This inspects the first token of a string a shell will later
        re-interpret. It encodes operator intent against careless behaviour; it
        does not contain a process, and ``sh -c``, pipelines, substitutions and
        an absolute path to a renamed binary all step around it. Real containment
        is the runner's axis, never this one.
        """
        if "run_command" not in self._present:
            return PolicyVerdict(
                decision=PolicyDecision.UNGATED,
                reason=self._annotate("no run_command policy is in effect"),
            )

        token = _first_token(command)
        if token is None:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                reason=self._annotate(
                    "run_command denied: empty command has no program token to approve"
                ),
                matched_rule="run_command",
            )

        deny = _str_list(self._run_command.get("deny"))
        if token in deny:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                reason=self._annotate(f"run_command denied: {token!r} is on the deny list"),
                matched_rule="run_command.deny",
            )

        allow = _str_list(self._run_command.get("allow"))
        if allow and token not in allow:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                reason=self._annotate(f"run_command denied: {token!r} is not on the allow list"),
                matched_rule="run_command.allow",
            )

        return PolicyVerdict(
            decision=PolicyDecision.ALLOWED,
            reason=self._annotate(""),
            matched_rule="run_command.allow" if allow else "run_command",
        )

    def check_file(self, category: str, name: str, path: str | Path) -> PolicyVerdict:
        """Gate a hook or command file by name plus content checksum.

        *category* is one of :data:`FILE_CATEGORIES`. *name* is the lookup key in
        whatever convention the caller records — the first consumer keys ``hooks``
        by repo-relative path and ``commands`` by stem, and this module preserves
        the key it is handed rather than deriving one. *path* is the on-disk file
        to verify.

        Semantics:

        * an absent section, or an unrecognised *category*, is ``UNGATED``;
        * otherwise, no entry for *name* is denied — allow-list semantics mean an
          unlisted file is not approved;
        * an entry is verified against *path*; a match is allowed, a mismatch or
          an unreadable file is denied, because content that changed voids the
          approval and a file that cannot be read cannot be approved.

        This gate is deliberately **not** applied to ordinary structured file
        operations. Confining those is the filesystem layer's job, and routing
        every read and write through a checksum allow-list would be a different
        product.
        """
        if category not in FILE_CATEGORIES or category not in self._present:
            return PolicyVerdict(
                decision=PolicyDecision.UNGATED,
                reason=self._annotate(f"no {category} policy is in effect"),
            )

        approval = self._files.get(category, {}).get(name)
        if approval is None:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                reason=self._annotate(
                    f"{category} denied: {name!r} is not approved "
                    f"(no entry in {POLICY_FILENAME})"
                ),
                matched_rule=f"{category}.unlisted",
            )

        if verify_checksum(path, approval):
            return PolicyVerdict(
                decision=PolicyDecision.ALLOWED,
                reason=self._annotate(""),
                matched_rule=f"{category}.{name}",
            )

        return PolicyVerdict(
            decision=PolicyDecision.DENIED,
            reason=self._annotate(
                f"{category} denied: {name!r} content changed / approval void "
                "(checksum mismatch or file unreadable)"
            ),
            matched_rule=f"{category}.{name}",
        )

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The JSON-serializable record of this snapshot.

        The key set is fixed. An empty policy and a policy loaded from candidates
        that declared nothing produce the same keys, differing only in the values
        that describe where they looked.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "present": sorted(self._present),
            "empty": self.is_empty(),
            "run_command": dict(self._run_command),
            "files": {category: dict(entries) for category, entries in sorted(self._files.items())},
            "sources": [source.to_dict() for source in self._sources],
            "source_root": None if self._source_root is None else str(self._source_root),
            "roots_are_separate": self._roots_are_separate,
            "degraded": self.degraded,
            "unresolved": self.unresolved,
            "trustworthy": self.trustworthy,
            "trust_note": self.trust_note,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _as_candidate(value: str | Path | PolicyCandidate) -> PolicyCandidate:
    if isinstance(value, PolicyCandidate):
        return value
    return PolicyCandidate(path=Path(value))


def load_policy(
    candidates: Sequence[str | Path | PolicyCandidate] = (),
    *,
    data: Mapping[str, Any] | None = None,
) -> Policy:
    """Build a :class:`Policy` from **pre-resolved** candidate paths and inline data.

    *candidates* are ordered by increasing precedence: the last one wins for the
    sections it defines. *data* is an inline policy document and takes precedence
    over every file. Both are optional, and with neither the result is the empty
    policy.

    **Merge is whole-section replacement, never a deep merge.** A later candidate
    that redefines ``run_command`` replaces it entirely; sections only an earlier
    candidate defines survive untouched. A section counts as **present** — and
    therefore gates — when *any* contributing source defines it.

    Nothing is searched for. This function does not know what a config directory
    is, does not construct per-model paths, and does not glob: the caller resolved
    those already, which is exactly what keeps its layout semantics out of here.

    Never raises for a bad source. An absent, unreadable or malformed candidate
    contributes no sections and is recorded on :attr:`Policy.sources`, which is
    how the resulting policy can report that it is degraded rather than quietly
    behaving as though nothing was ever declared.
    """
    return _build(
        [_as_candidate(value) for value in candidates],
        data=data,
        source_root=None,
        roots_are_separate=True,
    )


def _build(
    candidates: Sequence[PolicyCandidate],
    *,
    data: Mapping[str, Any] | None,
    source_root: Path | None,
    roots_are_separate: bool,
) -> Policy:
    """Read every candidate, merge whole sections in order, and assemble the policy.

    The single place a :class:`Policy` is constructed from sources, so
    :func:`load_policy` and :func:`snapshot` cannot drift into two merge rules.
    """
    sources = [_read_source(candidate) for candidate in candidates]

    if data is not None:
        sources.append(
            PolicySource(status=SourceStatus.INLINE, sections=_sections_from_object(dict(data)))
        )

    merged: dict[str, dict] = {}
    for source in sources:
        merged.update(source.sections)

    return Policy(
        run_command=merged.get("run_command", {}),
        files={category: _str_map(merged.get(category)) for category in FILE_CATEGORIES},
        present=frozenset(merged),
        sources=sources,
        source_root=source_root,
        roots_are_separate=roots_are_separate,
    )


def snapshot(
    environment: Any,
    candidates: Sequence[str | Path | PolicyCandidate] = (POLICY_FILENAME,),
    *,
    data: Mapping[str, Any] | None = None,
) -> Policy:
    """Snapshot policy from *environment*'s trusted source root.

    Every candidate is interpreted **relative to**
    :attr:`~shell.environment.Environment.source_root` and must resolve inside
    it. This is the whole mechanism behind one property: an operation cannot
    change its own active authorization, because the only tree it may write is
    the work root and the only tree policy is read from is the source root.

    Three candidates are refused with :class:`PolicySourceError` rather than
    loaded, because each would hand that property away:

    * an absolute path — it names a tree the environment never vouched for;
    * a path that escapes the source root via ``..``;
    * a path that resolves inside the work root, which can happen legitimately
      when the work root is nested under the source root, and which would put
      the policy file back within reach of the operations it governs.

    Refusing loudly is deliberate. A caller passing an unsafe candidate has a bug,
    and degrading that bug to an ungated no-op would delete the gate at the exact
    moment someone was trying to configure it.

    When a deployment points both roots at the same directory the separation does
    not exist. That is recorded on :attr:`Policy.roots_are_separate` and is not
    treated as an error here — it is the deployment's choice to make, and the
    consumer is told rather than overruled.
    """
    source_root = Path(environment.source_root).resolve()
    work_root = Path(environment.work_root).resolve()
    roots_are_separate = source_root != work_root

    resolved: list[PolicyCandidate] = []
    for value in candidates:
        candidate = _as_candidate(value)
        raw = candidate.path

        if raw.is_absolute():
            raise PolicySourceError(
                f"policy candidate {str(raw)!r} is absolute; candidates are resolved "
                f"relative to the source root ({source_root}) so that policy can only "
                "ever come from trusted control context"
            )

        target = (source_root / raw).resolve()
        if target != source_root and source_root not in target.parents:
            raise PolicySourceError(
                f"policy candidate {str(raw)!r} escapes the source root ({source_root}); "
                "a policy read from outside trusted control context is not a gate"
            )

        if roots_are_separate and (target == work_root or work_root in target.parents):
            raise PolicySourceError(
                f"policy candidate {str(raw)!r} resolves inside the work root "
                f"({work_root}); an operation must not be able to edit its own "
                "active authorization"
            )

        resolved.append(PolicyCandidate(path=target, required=candidate.required))

    return _build(
        resolved,
        data=data,
        source_root=source_root,
        roots_are_separate=roots_are_separate,
    )


# ---------------------------------------------------------------------------
# Read-only path confinement
# ---------------------------------------------------------------------------


def check_write(path: str | Path, environment: Any) -> PolicyVerdict:
    """Gate a write against the environment's declared read-only subtrees.

    :attr:`~shell.environment.Environment.read_only_paths` generalises what the
    first consumer hard-coded as a single neighbour-clone directory: trees inside
    the work root that operations may read but must never write. Root confinement
    alone does not cover them, because they sit *within* the confined root — so
    every write path consults this in addition to resolving safely.

    Presence is the semantic here too, matching the rest of this module: an
    environment that declares no read-only paths returns ``UNGATED`` rather than
    ``ALLOWED``, so a consumer can tell "nothing was declared" from "a declared
    rule permitted this".

    Symlinks are resolved before comparison, so a link inside a writable tree
    cannot be used to land a write in a protected one.
    """
    read_only = tuple(environment.read_only_paths)
    if not read_only:
        return PolicyVerdict(
            decision=PolicyDecision.UNGATED,
            reason="no read-only paths are declared for this environment",
        )

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(environment.work_root) / candidate
    candidate = candidate.resolve()

    for protected in read_only:
        protected = Path(protected).resolve()
        if candidate == protected or protected in candidate.parents:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                reason=(
                    f"write refused: {str(path)!r} is inside the read-only path "
                    f"{str(protected)!r}. It may be read, never written."
                ),
                matched_rule=f"read_only:{protected}",
            )

    return PolicyVerdict(
        decision=PolicyDecision.ALLOWED,
        matched_rule="read_only",
    )
