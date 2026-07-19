"""Tests for the operation-policy evaluator.

The evaluator is a port of the first consumer's approval gate, and its semantics
are pinned here rather than re-derived: allow-list-when-present, deny-beats-allow,
first-shlex-token extraction, checksum approvals over ``sha256``/``md5``, and a
verifying side that never raises. Those behaviours have downstream tests
depending on them, so a change here is a compatibility break, not a refactor.

Three groups carry the weight beyond that port:

* **the three states** — absent, malformed, and expected-but-unresolved must
  stay separately observable, because collapsing them is how a gate stops
  gating without anyone noticing;
* **snapshot confinement** — policy comes from the source root, and an operation
  must not be able to reach its own authorization from the work root;
* **read-only paths** — the generalisation of a single hard-coded protected
  subtree into a declared set.

Parent/child policy composition is deliberately absent. A pending upstream
security fix changes composition semantics, and encoding today's behaviour as
the target would pin the bug.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shell.environment import Environment
from shell.policy import (
    DEFAULT_ALGO,
    FILE_CATEGORIES,
    POLICY_FILENAME,
    SUPPORTED_CHECKSUM_ALGOS,
    Policy,
    PolicyCandidate,
    PolicySourceError,
    SourceStatus,
    check_write,
    file_checksum,
    load_policy,
    snapshot,
    verify_checksum,
)
from shell.results import PolicyDecision
from shell.runners.host import HostRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_policy(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _env(source_root: Path, work_root: Path | None = None, **kwargs) -> Environment:
    source_root.mkdir(parents=True, exist_ok=True)
    if work_root is None:
        work_root = source_root
    work_root.mkdir(parents=True, exist_ok=True)
    return Environment(
        source_root=source_root,
        work_root=work_root,
        runner=HostRunner(),
        **kwargs,
    )


def _allowed(verdict) -> bool:
    """The ported evaluator's notion of "not denied", across UNGATED and ALLOWED."""
    return not verdict.denied


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def test_file_checksum_default_is_sha256(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello", encoding="utf-8")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert file_checksum(target) == f"sha256:{expected}"
    assert DEFAULT_ALGO == "sha256"


def test_file_checksum_md5_honored(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello", encoding="utf-8")
    assert file_checksum(target, "md5") == f"md5:{hashlib.md5(b'hello').hexdigest()}"  # nosec B324


def test_file_checksum_rejects_unsupported_algo(tmp_path: Path) -> None:
    """The authoring side surfaces a bad algorithm; only verification stays silent."""
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported checksum algorithm"):
        file_checksum(target, "sha1")


def test_supported_algos_are_stable_and_ordered() -> None:
    assert SUPPORTED_CHECKSUM_ALGOS == ("sha256", "md5")


@pytest.mark.parametrize("algo", SUPPORTED_CHECKSUM_ALGOS)
def test_verify_checksum_roundtrip(tmp_path: Path, algo: str) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content", encoding="utf-8")
    assert verify_checksum(target, file_checksum(target, algo)) is True


def test_verify_checksum_detects_a_changed_file(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("before", encoding="utf-8")
    approval = file_checksum(target)
    target.write_text("after", encoding="utf-8")
    assert verify_checksum(target, approval) is False


@pytest.mark.parametrize(
    "approval",
    ["", "nocolon", "sha1:abc", "sha256:", ":abc", 42, None],
)
def test_verify_checksum_never_raises_on_a_bad_approval(tmp_path: Path, approval: object) -> None:
    """Cannot verify means withhold approval — the only safe direction for a gate."""
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    assert verify_checksum(target, approval) is False  # type: ignore[arg-type]


def test_verify_checksum_missing_file_is_false(tmp_path: Path) -> None:
    assert verify_checksum(tmp_path / "absent.txt", f"sha256:{'0' * 64}") is False


# ---------------------------------------------------------------------------
# The empty policy is a total no-op
# ---------------------------------------------------------------------------


def test_empty_policy_is_empty_and_ungated() -> None:
    policy = Policy()
    assert policy.is_empty() is True
    assert policy.section_present("run_command") is False
    assert policy.run_command_config() is None
    assert policy.check_run_command("rm -rf /").decision is PolicyDecision.UNGATED
    assert policy.check_file("hooks", "lint.sh", "/nonexistent").decision is PolicyDecision.UNGATED


def test_no_candidates_matches_a_bare_empty_policy(tmp_path: Path) -> None:
    """An empty policy and a policy that found nothing agree on every decision."""
    bare = Policy()
    loaded = load_policy([tmp_path / "absent.json"])

    assert bare.is_empty() == loaded.is_empty() is True
    assert bare.to_dict().keys() == loaded.to_dict().keys()
    for command in ("git status", "", "rm -rf /"):
        assert (
            bare.check_run_command(command).decision == loaded.check_run_command(command).decision
        )
    assert bare.trustworthy is loaded.trustworthy is True


def test_to_dict_key_set_is_fixed_across_shapes(tmp_path: Path) -> None:
    """A populated, an empty and a degraded policy all serialize the same keys."""
    populated = load_policy(data={"run_command": {"allow": ["git"]}})
    broken = _write_policy(tmp_path / "bad.json", "x")
    broken.write_text("{not json", encoding="utf-8")
    degraded = load_policy([broken])

    assert Policy().to_dict().keys() == populated.to_dict().keys() == degraded.to_dict().keys()


def test_to_dict_is_json_serializable(tmp_path: Path) -> None:
    policy = load_policy(
        [PolicyCandidate(path=tmp_path / "absent.json", required=True)],
        data={"run_command": {"allow": ["git"]}},
    )
    assert json.loads(json.dumps(policy.to_dict()))["unresolved"] is True


# ---------------------------------------------------------------------------
# Presence, not emptiness, is the semantic
# ---------------------------------------------------------------------------


def test_absent_section_is_ungated_not_denying() -> None:
    """The load-bearing no-op: no section means the category is not gated at all."""
    policy = load_policy(data={"hooks": {"lint.sh": "sha256:deadbeef"}})
    verdict = policy.check_run_command("anything at all")
    assert verdict.decision is PolicyDecision.UNGATED
    assert _allowed(verdict)


def test_present_but_empty_section_still_gates() -> None:
    """A section that exists and lists nothing is present, active, and matches nothing."""
    policy = load_policy(data={"run_command": {}})
    assert policy.section_present("run_command") is True
    assert policy.run_command_config() == {}

    verdict = policy.check_run_command("git status")
    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.decision is not PolicyDecision.UNGATED


def test_ungated_is_distinct_from_allowed() -> None:
    """A consumer must be able to tell "no gate exists" from "a gate permitted it"."""
    ungated = Policy().check_run_command("git status")
    allowed = load_policy(data={"run_command": {"allow": ["git"]}}).check_run_command("git status")

    assert _allowed(ungated) and _allowed(allowed)
    assert ungated.decision is not allowed.decision


def test_empty_section_denies_nothing_but_reports_a_rule() -> None:
    policy = load_policy(data={"run_command": {"allow": [], "deny": []}})
    verdict = policy.check_run_command("rm -rf /")
    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.matched_rule == "run_command"


# ---------------------------------------------------------------------------
# run_command evaluation
# ---------------------------------------------------------------------------


def test_allow_list_denies_an_unlisted_token() -> None:
    policy = load_policy(data={"run_command": {"allow": ["git", "pytest"]}})
    assert _allowed(policy.check_run_command("git status"))
    denied = policy.check_run_command("curl http://example.com")
    assert denied.decision is PolicyDecision.DENIED
    assert "not on the allow list" in denied.reason
    assert denied.matched_rule == "run_command.allow"


def test_deny_list_blocks_a_listed_token() -> None:
    policy = load_policy(data={"run_command": {"deny": ["curl"]}})
    denied = policy.check_run_command("curl http://example.com")
    assert denied.decision is PolicyDecision.DENIED
    assert "on the deny list" in denied.reason
    assert denied.matched_rule == "run_command.deny"
    assert _allowed(policy.check_run_command("git status"))


def test_deny_beats_allow() -> None:
    """Ordering is load-bearing: deny is consulted first and wins outright."""
    policy = load_policy(data={"run_command": {"allow": ["git"], "deny": ["git"]}})
    verdict = policy.check_run_command("git status")
    assert verdict.decision is PolicyDecision.DENIED
    assert verdict.matched_rule == "run_command.deny"


def test_token_is_the_first_shlex_token() -> None:
    policy = load_policy(data={"run_command": {"allow": ["git"]}})
    assert _allowed(policy.check_run_command("  git   commit -m 'a message'  "))
    assert policy.check_run_command("'git' status").denied is False


@pytest.mark.parametrize("command", ["", "   ", "\n\t", "'unbalanced"])
def test_no_parseable_token_is_denied_under_an_active_section(command: str) -> None:
    """An unlexable command yields no token, and a gate that cannot see a token denies."""
    policy = load_policy(data={"run_command": {"allow": ["git"]}})
    verdict = policy.check_run_command(command)
    assert verdict.decision is PolicyDecision.DENIED
    assert "no program token" in verdict.reason


def test_no_parseable_token_is_ungated_when_the_section_is_absent() -> None:
    assert Policy().check_run_command("").decision is PolicyDecision.UNGATED


def test_the_gate_reads_one_token_and_says_so() -> None:
    """A shell re-interprets the string afterwards; the gate sees only the first token.

    Pinned as a test rather than left to prose because someone will eventually
    read the allow-list as containment. It is not: this passes.
    """
    policy = load_policy(data={"run_command": {"allow": ["sh"], "deny": ["curl"]}})
    assert _allowed(policy.check_run_command("sh -c 'curl http://example.com'"))


@pytest.mark.parametrize("bad", [{"allow": "git"}, {"allow": None}, {"allow": [1, 2]}])
def test_malformed_allow_list_degrades_to_no_entries(bad: dict) -> None:
    """A bad list shape must not gate unexpectedly, and must not raise."""
    policy = load_policy(data={"run_command": bad})
    assert _allowed(policy.check_run_command("anything"))


def test_non_string_members_are_dropped_from_a_list() -> None:
    policy = load_policy(data={"run_command": {"allow": ["git", 7, None, "uv"]}})
    assert _allowed(policy.check_run_command("uv sync"))
    assert policy.check_run_command("curl x").denied is True


# ---------------------------------------------------------------------------
# check_file evaluation
# ---------------------------------------------------------------------------


def test_check_file_approves_a_matching_checksum(tmp_path: Path) -> None:
    hook = tmp_path / "lint.sh"
    hook.write_text("#!/bin/sh\necho lint\n", encoding="utf-8")
    policy = load_policy(data={"hooks": {"lint.sh": file_checksum(hook)}})

    verdict = policy.check_file("hooks", "lint.sh", hook)
    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.matched_rule == "hooks.lint.sh"


def test_check_file_denies_changed_content(tmp_path: Path) -> None:
    hook = tmp_path / "lint.sh"
    hook.write_text("original", encoding="utf-8")
    policy = load_policy(data={"hooks": {"lint.sh": file_checksum(hook)}})
    hook.write_text("tampered", encoding="utf-8")

    verdict = policy.check_file("hooks", "lint.sh", hook)
    assert verdict.decision is PolicyDecision.DENIED
    assert "approval void" in verdict.reason


def test_check_file_denies_an_unlisted_name(tmp_path: Path) -> None:
    """Allow-list semantics: an unlisted file is not approved."""
    other = tmp_path / "other.sh"
    other.write_text("x", encoding="utf-8")
    policy = load_policy(data={"hooks": {"lint.sh": "sha256:" + "0" * 64}})

    verdict = policy.check_file("hooks", "other.sh", other)
    assert verdict.decision is PolicyDecision.DENIED
    assert "is not approved" in verdict.reason
    assert POLICY_FILENAME in verdict.reason
    assert verdict.matched_rule == "hooks.unlisted"


def test_check_file_denies_a_missing_file(tmp_path: Path) -> None:
    policy = load_policy(data={"hooks": {"lint.sh": f"sha256:{'0' * 64}"}})
    assert policy.check_file("hooks", "lint.sh", tmp_path / "gone.sh").denied is True


def test_check_file_commands_category(tmp_path: Path) -> None:
    """``commands`` is keyed by stem where ``hooks`` is keyed by path — the key travels as given."""
    command = tmp_path / "fix-lint.md"
    command.write_text("body", encoding="utf-8")
    policy = load_policy(data={"commands": {"fix-lint": file_checksum(command)}})

    assert _allowed(policy.check_file("commands", "fix-lint", command))
    assert policy.check_file("commands", "fix-lint.md", command).denied is True


def test_check_file_unknown_category_is_ungated(tmp_path: Path) -> None:
    policy = load_policy(data={"hooks": {}})
    verdict = policy.check_file("plugins", "anything", tmp_path / "x")
    assert verdict.decision is PolicyDecision.UNGATED


def test_check_file_absent_category_is_ungated(tmp_path: Path) -> None:
    policy = load_policy(data={"run_command": {"allow": ["git"]}})
    assert policy.check_file("hooks", "lint.sh", tmp_path / "x").decision is PolicyDecision.UNGATED


def test_file_categories_are_the_two_known_ones() -> None:
    assert FILE_CATEGORIES == frozenset({"hooks", "commands"})


def test_file_approval_reads_the_merged_view(tmp_path: Path) -> None:
    base = _write_policy(tmp_path / "base.json", {"hooks": {"a.sh": "sha256:aaa"}})
    overlay = _write_policy(tmp_path / "overlay.json", {"hooks": {"b.sh": "sha256:bbb"}})
    policy = load_policy([base, overlay])

    # Whole-section replacement: the overlay's hooks section wins entirely.
    assert policy.file_approval("hooks", "b.sh") == "sha256:bbb"
    assert policy.file_approval("hooks", "a.sh") is None
    assert policy.file_approval("commands", "anything") is None


def test_structured_file_operations_are_not_checksum_gated(tmp_path: Path) -> None:
    """The carve-out: check_file gates hooks and commands, not ordinary file work.

    Routing every read and write through a checksum allow-list would be a
    different product. Confining file operations is the filesystem layer's job.
    """
    policy = load_policy(data={"hooks": {"lint.sh": "sha256:" + "0" * 64}})
    for category in ("fs.read", "fs.write", "read_file", "write_file"):
        assert policy.check_file(category, "any.txt", tmp_path / "any.txt").denied is False


# ---------------------------------------------------------------------------
# Loading and merging from pre-resolved candidates
# ---------------------------------------------------------------------------


def test_load_policy_resolves_nothing_of_its_own(tmp_path: Path) -> None:
    """Only the paths handed in are read — there is no search order here."""
    _write_policy(tmp_path / POLICY_FILENAME, {"run_command": {"deny": ["curl"]}})
    policy = load_policy()

    assert policy.is_empty() is True
    assert policy.sources == ()
    assert _allowed(policy.check_run_command("curl x"))


def test_later_candidates_win_per_section(tmp_path: Path) -> None:
    base = _write_policy(tmp_path / "base.json", {"run_command": {"allow": ["git"]}})
    overlay = _write_policy(tmp_path / "overlay.json", {"run_command": {"allow": ["uv"]}})
    policy = load_policy([base, overlay])

    assert _allowed(policy.check_run_command("uv sync"))
    assert policy.check_run_command("git status").denied is True


def test_merge_is_whole_section_never_deep(tmp_path: Path) -> None:
    """An overlay redefining a section replaces it outright, keys and all."""
    base = _write_policy(tmp_path / "base.json", {"run_command": {"allow": ["sh"], "deny": ["uv"]}})
    overlay = _write_policy(tmp_path / "overlay.json", {"run_command": {"allow": ["uv"]}})
    policy = load_policy([base, overlay])

    # The base's ``deny`` key is gone entirely, not merged under the new allow.
    assert policy.run_command_config() == {"allow": ["uv"]}
    assert _allowed(policy.check_run_command("uv sync"))
    # ...and the base's allow entry went with it.
    denied = policy.check_run_command("sh -c x")
    assert denied.matched_rule == "run_command.allow"


def test_sections_only_an_earlier_candidate_defines_survive(tmp_path: Path) -> None:
    base = _write_policy(tmp_path / "base.json", {"hooks": {"a.sh": "sha256:aaa"}})
    overlay = _write_policy(tmp_path / "overlay.json", {"run_command": {"allow": ["git"]}})
    policy = load_policy([base, overlay])

    assert policy.section_present("hooks") is True
    assert policy.section_present("run_command") is True
    assert policy.file_approval("hooks", "a.sh") == "sha256:aaa"


def test_inline_data_wins_over_every_file(tmp_path: Path) -> None:
    base = _write_policy(tmp_path / "base.json", {"run_command": {"allow": ["git"]}})
    policy = load_policy([base], data={"run_command": {"allow": ["uv"]}})

    assert _allowed(policy.check_run_command("uv sync"))
    assert policy.check_run_command("git status").denied is True


def test_a_section_is_present_if_any_source_defines_it(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    base = _write_policy(tmp_path / "base.json", {"run_command": {}})
    policy = load_policy([missing, base])
    assert policy.section_present("run_command") is True


@pytest.mark.parametrize("document", ["[]", '"a string"', "null", "42"])
def test_a_non_object_document_is_malformed(tmp_path: Path, document: str) -> None:
    path = tmp_path / "p.json"
    path.write_text(document, encoding="utf-8")
    policy = load_policy([path])

    assert policy.is_empty() is True
    assert policy.sources[0].status is SourceStatus.MALFORMED


def test_non_object_sections_are_dropped(tmp_path: Path) -> None:
    """A wrong-shaped section reads as absent rather than gating unexpectedly."""
    path = _write_policy(
        tmp_path / "p.json",
        {"run_command": "not an object", "hooks": ["nope"], "commands": None},
    )
    policy = load_policy([path])

    assert policy.is_empty() is True
    assert policy.section_present("run_command") is False
    assert _allowed(policy.check_run_command("anything"))


def test_candidates_accept_str_path_and_candidate(tmp_path: Path) -> None:
    path = _write_policy(tmp_path / "p.json", {"run_command": {"allow": ["git"]}})
    for value in (str(path), path, PolicyCandidate(path=path)):
        assert load_policy([value]).check_run_command("curl x").denied is True


# ---------------------------------------------------------------------------
# Three distinct states: absent / malformed / expected-but-unresolved
# ---------------------------------------------------------------------------


def test_absent_source_is_absent_and_trustworthy(tmp_path: Path) -> None:
    policy = load_policy([tmp_path / "absent.json"])
    assert policy.sources[0].status is SourceStatus.ABSENT
    assert policy.degraded is False
    assert policy.unresolved is False
    assert policy.trustworthy is True
    assert policy.trust_note == ""


def test_malformed_source_degrades_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("{ not json at all", encoding="utf-8")
    policy = load_policy([path])

    assert policy.is_empty() is True
    assert policy.sources[0].status is SourceStatus.MALFORMED
    assert policy.degraded is True
    assert policy.trustworthy is False


def test_unreadable_source_is_its_own_status(tmp_path: Path) -> None:
    """A directory where a file was expected is unreadable, not absent."""
    path = tmp_path / "p.json"
    path.mkdir()
    policy = load_policy([path])

    assert policy.sources[0].status is SourceStatus.UNREADABLE
    assert policy.degraded is True


def test_expected_but_unresolved_is_distinct_from_absent(tmp_path: Path) -> None:
    """A required candidate that never arrived is not the same as nothing declared."""
    optional = load_policy([tmp_path / "absent.json"])
    required = load_policy([PolicyCandidate(path=tmp_path / "absent.json", required=True)])

    assert optional.sources[0].status is required.sources[0].status is SourceStatus.ABSENT
    assert optional.unresolved is False
    assert required.unresolved is True
    assert optional.trustworthy is True
    assert required.trustworthy is False


def test_the_three_states_are_mutually_distinguishable(tmp_path: Path) -> None:
    """All three produce an empty policy, and none is mistakable for another."""
    absent = load_policy([tmp_path / "gone.json"])

    broken = tmp_path / "broken.json"
    broken.write_text("{{{", encoding="utf-8")
    malformed = load_policy([broken])

    unresolved = load_policy([PolicyCandidate(path=tmp_path / "gone.json", required=True)])

    assert absent.is_empty() and malformed.is_empty() and unresolved.is_empty()
    signatures = {
        (p.degraded, p.unresolved, p.trustworthy) for p in (absent, malformed, unresolved)
    }
    assert len(signatures) == 3


def test_a_degraded_gate_is_never_silent(tmp_path: Path) -> None:
    """The decision matches the absent case; the silence does not.

    A corrupt policy file must not abort a run, so it still degrades to a no-op.
    What it must not do is look exactly like a repository that never declared a
    gate — every verdict names the problem.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{ oops", encoding="utf-8")
    policy = load_policy([broken])

    verdict = policy.check_run_command("curl http://example.com")
    assert verdict.decision is PolicyDecision.UNGATED
    assert "policy degraded" in verdict.reason
    assert str(broken) in verdict.reason
    assert policy.to_dict()["trustworthy"] is False


def test_the_degradation_marker_reaches_every_check(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("nope", encoding="utf-8")
    policy = load_policy([broken], data={"run_command": {"allow": ["git"]}, "hooks": {}})

    for verdict in (
        policy.check_run_command("git status"),
        policy.check_run_command("curl x"),
        policy.check_file("hooks", "lint.sh", tmp_path / "lint.sh"),
    ):
        assert "policy degraded" in verdict.reason


def test_a_malformed_declared_gate_does_not_become_allow_all_silently(tmp_path: Path) -> None:
    """The failure mode this whole state machine exists to prevent.

    An operator wrote a gate; the file is corrupt. The evaluator does not raise
    and does not invent a denial, but a caller reading ``trustworthy`` can refuse
    — which is the point of exposing it rather than deciding here.
    """
    broken = tmp_path / POLICY_FILENAME
    broken.write_text('{"run_command": {"deny": ["curl"]}', encoding="utf-8")
    policy = load_policy([PolicyCandidate(path=broken, required=True)])

    assert policy.trustworthy is False
    assert policy.degraded is True
    assert "is not valid JSON" in policy.trust_note


def test_a_trustworthy_policy_carries_no_annotation() -> None:
    policy = load_policy(data={"run_command": {"allow": ["git"]}})
    assert policy.check_run_command("git status").reason == ""
    assert policy.check_run_command("curl x").reason.endswith("not on the allow list")


# ---------------------------------------------------------------------------
# Snapshotting from trusted control context
# ---------------------------------------------------------------------------


def test_snapshot_reads_from_the_source_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    env = _env(source, work)
    _write_policy(source / POLICY_FILENAME, {"run_command": {"deny": ["curl"]}})

    policy = snapshot(env)
    assert policy.check_run_command("curl x").denied is True
    assert policy.source_root == source.resolve()
    assert policy.roots_are_separate is True


def test_snapshot_ignores_a_policy_file_in_the_work_root(tmp_path: Path) -> None:
    """The core property: an operation cannot author its own authorization.

    A permissive policy written into the tree the operation may change has no
    effect, because policy is only ever read from the source root.
    """
    source = tmp_path / "source"
    work = tmp_path / "work"
    env = _env(source, work)

    _write_policy(source / POLICY_FILENAME, {"run_command": {"allow": ["git"]}})
    # What a model with write access to the work root would try.
    _write_policy(work / POLICY_FILENAME, {"run_command": {"allow": ["git", "curl", "sh"]}})

    policy = snapshot(env)
    assert policy.check_run_command("curl http://example.com").denied is True
    assert [str(s.path) for s in policy.sources] == [str((source / POLICY_FILENAME).resolve())]


def test_snapshot_refuses_an_absolute_candidate(tmp_path: Path) -> None:
    env = _env(tmp_path / "source", tmp_path / "work")
    with pytest.raises(PolicySourceError, match="is absolute"):
        snapshot(env, [tmp_path / "elsewhere.json"])


def test_snapshot_refuses_a_candidate_that_escapes_the_source_root(tmp_path: Path) -> None:
    env = _env(tmp_path / "source", tmp_path / "work")
    with pytest.raises(PolicySourceError, match="escapes the source root"):
        snapshot(env, ["../work/approvals.json"])


def test_snapshot_refuses_a_candidate_inside_a_nested_work_root(tmp_path: Path) -> None:
    """The work root may legitimately be nested under the source root.

    Confinement to the source root alone would then admit a candidate the
    operation can write, so the work root is excluded explicitly.
    """
    source = tmp_path / "source"
    work = source / "worktrees" / "task-1"
    env = _env(source, work)

    with pytest.raises(PolicySourceError, match="resolves inside the work root"):
        snapshot(env, ["worktrees/task-1/approvals.json"])

    # A sibling path under the same source root is fine.
    assert snapshot(env, ["config/approvals.json"]).is_empty() is True


def test_snapshot_refusal_is_loud_rather_than_ungated(tmp_path: Path) -> None:
    """An unsafe candidate is a caller bug; degrading it would delete the gate."""
    env = _env(tmp_path / "source", tmp_path / "work")
    with pytest.raises(PolicySourceError):
        snapshot(env, ["../outside.json"])


def test_snapshot_records_shared_roots_without_failing(tmp_path: Path) -> None:
    """One directory for both roots is a deployment choice, reported not overruled."""
    shared = tmp_path / "repo"
    env = _env(shared)
    _write_policy(shared / POLICY_FILENAME, {"run_command": {"allow": ["git"]}})

    policy = snapshot(env)
    assert policy.roots_are_separate is False
    assert policy.to_dict()["roots_are_separate"] is False
    assert policy.check_run_command("curl x").denied is True


def test_snapshot_defaults_to_the_conventional_filename(tmp_path: Path) -> None:
    env = _env(tmp_path / "source", tmp_path / "work")
    policy = snapshot(env)
    assert [Path(s.path).name for s in policy.sources] == [POLICY_FILENAME]


def test_snapshot_accepts_ordered_candidates_and_inline_data(tmp_path: Path) -> None:
    source = tmp_path / "source"
    env = _env(source, tmp_path / "work")
    _write_policy(source / "base.json", {"run_command": {"allow": ["git"]}})
    _write_policy(source / "overlay.json", {"hooks": {}})

    policy = snapshot(env, ["base.json", "overlay.json"], data={"commands": {}})
    assert sorted(policy.to_dict()["present"]) == ["commands", "hooks", "run_command"]


def test_snapshot_propagates_required_candidates(tmp_path: Path) -> None:
    env = _env(tmp_path / "source", tmp_path / "work")
    policy = snapshot(env, [PolicyCandidate(path=Path(POLICY_FILENAME), required=True)])
    assert policy.unresolved is True


def test_snapshot_is_a_point_in_time_snapshot(tmp_path: Path) -> None:
    """Later edits to the source file do not retroactively change a taken snapshot."""
    source = tmp_path / "source"
    env = _env(source, tmp_path / "work")
    _write_policy(source / POLICY_FILENAME, {"run_command": {"allow": ["git"]}})

    policy = snapshot(env)
    _write_policy(source / POLICY_FILENAME, {"run_command": {"allow": ["git", "curl"]}})

    assert policy.check_run_command("curl x").denied is True


# ---------------------------------------------------------------------------
# Read-only paths
# ---------------------------------------------------------------------------


def test_check_write_denies_a_write_into_a_read_only_subtree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(protected,))

    verdict = check_write(protected / "peer" / "file.py", env)
    assert verdict.decision is PolicyDecision.DENIED
    assert "read-only" in verdict.reason
    assert verdict.matched_rule.startswith("read_only:")


def test_check_write_denies_the_read_only_root_itself(tmp_path: Path) -> None:
    work = tmp_path / "work"
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(protected,))
    assert check_write(protected, env).denied is True


def test_check_write_allows_a_path_outside_the_read_only_subtree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(protected,))

    verdict = check_write(work / "src" / "main.py", env)
    assert verdict.decision is PolicyDecision.ALLOWED
    assert verdict.matched_rule == "read_only"


def test_check_write_is_ungated_when_nothing_is_declared(tmp_path: Path) -> None:
    """Presence, not emptiness — consistent with every other gate in this module."""
    env = _env(tmp_path / "source", tmp_path / "work")
    verdict = check_write(tmp_path / "work" / "any.py", env)
    assert verdict.decision is PolicyDecision.UNGATED
    assert verdict.decision is not PolicyDecision.ALLOWED


def test_check_write_resolves_relative_paths_against_the_work_root(tmp_path: Path) -> None:
    work = tmp_path / "work"
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(protected,))
    assert check_write("neighbours/peer/x.py", env).denied is True


def test_check_write_follows_symlinks_before_comparing(tmp_path: Path) -> None:
    """A link in a writable tree must not be a way into a protected one."""
    work = tmp_path / "work"
    protected = work / "neighbours"
    protected.mkdir(parents=True)
    (work / "src").mkdir()
    link = work / "src" / "sneaky"
    link.symlink_to(protected, target_is_directory=True)

    env = _env(tmp_path / "source", work, read_only_paths=(protected,))
    assert check_write(link / "peer.py", env).denied is True


def test_check_write_generalises_beyond_one_subtree(tmp_path: Path) -> None:
    """The point of the generalisation: a set, not a single hard-coded directory."""
    work = tmp_path / "work"
    first = work / "neighbours"
    second = work / "vendor"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(first, second))

    assert check_write(first / "a", env).denied is True
    assert check_write(second / "b", env).denied is True
    assert check_write(work / "c", env).denied is False


def test_check_write_does_not_deny_a_similarly_named_sibling(tmp_path: Path) -> None:
    """Prefix matching on strings would deny ``neighbours-notes``; path parts do not."""
    work = tmp_path / "work"
    protected = work / "neighbours"
    sibling = work / "neighbours-notes"
    protected.mkdir(parents=True)
    sibling.mkdir(parents=True)
    env = _env(tmp_path / "source", work, read_only_paths=(protected,))

    assert check_write(sibling / "x.md", env).denied is False


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_policy_module_imports_stdlib_and_shell_only() -> None:
    import ast

    source = Path("shell/policy.py").resolve()
    if not source.exists():
        source = Path(__file__).resolve().parents[1] / "shell" / "policy.py"

    import sys

    names: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    offenders = sorted(n for n in names if n not in sys.stdlib_module_names and n != "shell")
    assert not offenders, f"shell/policy.py imports non-stdlib modules: {offenders}"


def test_policy_does_not_import_the_consumer() -> None:
    source = Path(__file__).resolve().parents[1] / "shell" / "policy.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("import colleague", "from colleague", "configdir", "sanitize_model"):
        assert forbidden not in text, f"shell/policy.py references {forbidden!r}"
