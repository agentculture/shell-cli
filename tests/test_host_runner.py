"""Execution semantics of the host runner.

These tests run **real processes**. Mocking ``subprocess`` here would test that
the runner calls the functions this file expects it to call, which is precisely
the thing that cannot go wrong in an interesting way. What can go wrong is that a
grandchild outlives the operation, that a bounded read deadlocks on a full pipe,
or that an escalation reports a success it did not achieve — and none of those
are observable through a mock.

Three groups matter most:

* **Orphan prevention.** ``test_the_bug_this_exists_to_prevent`` reproduces the
  failure with plain ``subprocess`` first, so the later assertions are anchored
  to a real defect rather than to a mechanism nobody showed was needed.
* **Escalation reporting.** A process that ignores the polite signal must show
  both steps, in order, with ``escalated`` set.
* **Honest gaps.** A descendant that leaves the process group survives, and the
  outcome has to say so rather than presenting a clean kill.

Timings are short but not zero. Values below are chosen so the suite stays
around a second while leaving enough slack that a loaded CI box does not turn a
correct implementation red.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from shell.runners import ProcessOutcome, ProcessRequest, Runner
from shell.runners.host import (
    HOST_ISOLATION_NOTE,
    ORPHAN_PREVENTION,
    HostRunner,
    process_group_supported,
)

posix_only = pytest.mark.skipif(
    not process_group_supported(),
    reason="POSIX process groups unavailable on this platform",
)


def _alive(pid: int) -> bool:
    """Whether *pid* still exists. Signal 0 checks without delivering anything."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def _read_pid(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return int(text)
        time.sleep(0.02)
    raise AssertionError(f"no pid was ever written to {path}")


def _reap(pid: int) -> None:
    """Best-effort cleanup so a failing test does not leak a sleeping process."""
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _fast() -> HostRunner:
    """A runner whose graces are short enough to test but long enough to be real."""
    return HostRunner(
        terminate_grace_seconds=0.3,
        kill_grace_seconds=1.0,
        drain_grace_seconds=0.3,
    )


# --- the request contract ---------------------------------------------------


def test_a_request_sets_exactly_one_of_argv_and_command() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ProcessRequest()
    with pytest.raises(ValueError, match="exactly one"):
        ProcessRequest(argv=("echo", "hi"), command="echo hi")


def test_a_blank_command_is_rejected_rather_than_run() -> None:
    with pytest.raises(ValueError, match="blank"):
        ProcessRequest(command="   ")


def test_a_request_records_env_names_but_never_env_values() -> None:
    payload = ProcessRequest(argv=("true",), env={"TOKEN": "s3cret"}).to_dict()
    assert payload["env_names"] == ["TOKEN"]
    assert "s3cret" not in repr(payload)


def test_uses_shell_distinguishes_a_vector_from_a_string() -> None:
    assert ProcessRequest(command="echo hi").uses_shell is True
    assert ProcessRequest(argv=("echo", "hi")).uses_shell is False


# --- ordinary execution -----------------------------------------------------


def test_the_host_runner_satisfies_the_runner_protocol() -> None:
    assert isinstance(HostRunner(), Runner)
    assert callable(HostRunner().run_process)


def test_an_argv_vector_runs_and_reports_its_streams_separately() -> None:
    outcome = HostRunner().run_process(
        ProcessRequest(
            argv=(sys.executable, "-c", "import sys; sys.stdout.write('o'); sys.stderr.write('e')")
        )
    )
    assert outcome.exit_code == 0
    assert outcome.stdout == "o"
    assert outcome.stderr == "e"
    assert outcome.succeeded is True


def test_stdout_and_stderr_are_never_merged() -> None:
    """The neutral record keeps the distinction; concatenating is the adapter's job."""
    outcome = HostRunner().run_process(ProcessRequest(command="echo out; echo err 1>&2; echo out2"))
    assert "err" not in outcome.stdout
    assert "out" not in outcome.stderr
    assert outcome.stdout.split() == ["out", "out2"]


def test_a_non_zero_exit_is_an_outcome_not_an_exception() -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="exit 3"))
    assert outcome.exit_code == 3
    assert outcome.succeeded is False
    assert outcome.timed_out is False
    assert outcome.termination.terminated is False


def test_a_command_that_cannot_start_is_an_outcome_with_a_reason() -> None:
    outcome = HostRunner().run_process(ProcessRequest(argv=("/nonexistent/definitely-not-here",)))
    assert outcome.exit_code is None
    assert "FileNotFoundError" in outcome.error
    assert outcome.succeeded is False


def test_cwd_is_honoured_and_recorded(tmp_path: Path) -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="pwd", cwd=tmp_path))
    assert outcome.stdout.strip() == str(tmp_path)
    assert outcome.cwd == str(tmp_path)


def test_an_explicit_env_replaces_rather_than_extends_the_inherited_one() -> None:
    """The runner adds nothing back. A handler's declared policy stays true."""
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", "import os; print(sorted(os.environ))"), env={})
    )
    # Some interpreters set a variable or two for themselves; what matters is
    # that nothing was inherited wholesale from this test process.
    assert "PATH" not in outcome.stdout


def test_stdin_is_delivered_and_the_pipe_closed() -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="cat", stdin="hello"))
    assert outcome.stdout == "hello"
    assert outcome.exit_code == 0


def test_a_command_reading_stdin_without_input_gets_eof_not_a_hang() -> None:
    outcome = HostRunner().run_process(
        ProcessRequest(command="cat", timeout_seconds=5.0),
    )
    assert outcome.exit_code == 0
    assert outcome.timed_out is False


def test_a_command_that_never_reads_its_stdin_is_not_an_error() -> None:
    """A closed pipe is the command's business, not a failure to run it."""
    outcome = HostRunner().run_process(
        ProcessRequest(command="true", stdin="x" * 200_000, timeout_seconds=10.0)
    )
    assert outcome.exit_code == 0
    assert outcome.error == ""


# --- bounded output ---------------------------------------------------------


def test_output_is_bounded_and_the_original_size_is_reported() -> None:
    script = "import sys; sys.stdout.write('x' * 5000)"
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", script), max_output_bytes=100)
    )
    assert outcome.stdout_truncated is True
    assert len(outcome.stdout) == 100
    assert outcome.stdout_bytes == 5000, "the byte count must describe the original stream"


def test_each_stream_is_bounded_independently() -> None:
    script = "import sys; sys.stdout.write('a' * 500); sys.stderr.write('b' * 10)"
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", script), max_output_bytes=50)
    )
    assert outcome.stdout_truncated is True
    assert outcome.stderr_truncated is False
    assert outcome.stderr == "b" * 10


def test_output_larger_than_a_pipe_buffer_does_not_deadlock() -> None:
    """A pipe holds ~64KB. Without concurrent draining this test hangs forever."""
    script = "import sys; sys.stdout.write('y' * 400_000)"
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", script), timeout_seconds=20.0)
    )
    assert outcome.exit_code == 0
    assert outcome.timed_out is False
    assert outcome.stdout_bytes == 400_000


def test_no_digest_of_the_raw_stream_is_produced_here() -> None:
    """Hashing happens downstream, after redaction and truncation.

    A digest of the unredacted stream would be a brute-force oracle for a short
    secret, so the runner deliberately produces none — it hands over text and
    counts, and evidence hashes what it finally stores.
    """
    payload = HostRunner().run_process(ProcessRequest(command="echo hi")).to_dict()
    assert not [key for key in payload if "sha" in key or "digest" in key or "hash" in key]


def test_truncation_on_a_byte_boundary_does_not_raise() -> None:
    """The bound counts bytes, so it can split a multi-byte character.

    Decoding with "replace" turns that into a visible replacement rather than a
    UnicodeDecodeError that would lose the whole captured stream over one glyph.
    """
    script = "import sys; sys.stdout.buffer.write('é'.encode() * 100)"
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", script), max_output_bytes=5)
    )
    assert outcome.stdout_truncated is True
    assert outcome.stdout.startswith("éé")
    assert outcome.stdout_bytes == 200


def test_an_unbounded_request_keeps_everything() -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="echo unbounded"))
    assert outcome.stdout_truncated is False
    assert outcome.stdout_bytes == len("unbounded\n")


# --- timeout ----------------------------------------------------------------


def test_a_timeout_is_its_own_state_not_a_failure() -> None:
    outcome = _fast().run_process(ProcessRequest(command="sleep 30", timeout_seconds=0.2))
    assert outcome.timed_out is True
    assert outcome.cancelled is False
    assert outcome.succeeded is False
    assert outcome.termination.reason == "timeout"


def test_a_timeout_terminates_before_the_command_would_have_finished() -> None:
    started = time.monotonic()
    outcome = _fast().run_process(ProcessRequest(command="sleep 30", timeout_seconds=0.2))
    assert time.monotonic() - started < 10.0
    assert outcome.termination.completed is True


def test_output_produced_before_a_timeout_is_still_captured() -> None:
    outcome = _fast().run_process(
        ProcessRequest(command="echo early; sleep 30", timeout_seconds=0.5)
    )
    assert outcome.timed_out is True
    assert "early" in outcome.stdout


def test_no_timeout_means_the_command_runs_to_completion() -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="echo done", timeout_seconds=None))
    assert outcome.exit_code == 0
    assert outcome.termination.terminated is False


# --- cancellation -----------------------------------------------------------


def test_cancellation_takes_the_same_escalation_path_as_a_timeout() -> None:
    cancel = threading.Event()
    runner = _fast()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        outcome = runner.run_process(
            ProcessRequest(command="sleep 30", timeout_seconds=30.0), cancel=cancel
        )
    finally:
        timer.cancel()
    assert outcome.cancelled is True
    assert outcome.timed_out is False
    assert outcome.termination.reason == "cancelled"
    assert outcome.termination.steps[0] == "SIGTERM"


def test_an_unset_cancel_event_changes_nothing() -> None:
    outcome = HostRunner().run_process(
        ProcessRequest(command="echo fine"), cancel=threading.Event()
    )
    assert outcome.exit_code == 0
    assert outcome.cancelled is False


def test_a_cancel_set_before_the_call_stops_the_command_promptly() -> None:
    cancel = threading.Event()
    cancel.set()
    outcome = _fast().run_process(
        ProcessRequest(command="sleep 30", timeout_seconds=30.0), cancel=cancel
    )
    assert outcome.cancelled is True


# --- escalation reporting ---------------------------------------------------


@posix_only
def test_a_cooperative_process_is_stopped_by_the_polite_signal_alone() -> None:
    outcome = _fast().run_process(ProcessRequest(command="sleep 30", timeout_seconds=0.2))
    assert outcome.termination.steps == ("SIGTERM",)
    assert outcome.termination.escalated is False
    assert outcome.termination.completed is True
    assert outcome.exit_code == -signal.SIGTERM, "the exit code names the signal that ended it"


@posix_only
def test_a_process_ignoring_sigterm_forces_the_escalation_and_it_is_recorded() -> None:
    """The escalation path is evidence: which signal, in what order, how far."""
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    outcome = _fast().run_process(
        ProcessRequest(argv=(sys.executable, "-c", script), timeout_seconds=0.3)
    )
    assert outcome.termination.steps == ("SIGTERM", "SIGKILL")
    assert outcome.termination.escalated is True
    assert outcome.termination.completed is True
    assert outcome.exit_code == -signal.SIGKILL


@posix_only
def test_the_escalation_path_survives_serialization() -> None:
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    payload = (
        _fast()
        .run_process(ProcessRequest(argv=(sys.executable, "-c", script), timeout_seconds=0.3))
        .to_dict()["termination"]
    )
    assert payload["steps"] == ["SIGTERM", "SIGKILL"]
    assert payload["escalated"] is True
    assert payload["completed"] is True
    assert payload["reason"] == "timeout"
    assert payload["group_signalled"] is True


def test_an_untouched_process_reports_an_empty_escalation_path() -> None:
    termination = HostRunner().run_process(ProcessRequest(command="true")).termination
    assert termination.steps == ()
    assert termination.terminated is False
    assert termination.group_signalled is False, "nothing was signalled, so nothing was"


# --- orphan prevention: the bug this exists to prevent ----------------------


@posix_only
def test_the_bug_this_exists_to_prevent(tmp_path: Path) -> None:
    """Killing only the direct child leaves the grandchild running.

    This asserts the *defect*, using plain ``subprocess`` the way a naive
    implementation would. Everything below is anchored to it: without this test
    the process-group machinery is a mechanism with no demonstrated need.
    """
    pidfile = tmp_path / "grandchild.pid"
    # Deliberately the naive path, not the runner: this reproduces the defect.
    child = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    grandchild = _read_pid(pidfile)
    try:
        child.kill()
        child.wait(timeout=5)
        time.sleep(0.2)
        assert _alive(grandchild), (
            "expected the naive path to orphan the grandchild; if this ever "
            "stops being true the runner's process group is solving nothing"
        )
    finally:
        _reap(grandchild)


@posix_only
def test_a_timeout_reaches_the_grandchild_not_only_the_child(tmp_path: Path) -> None:
    """The same shape as the baseline above, run through the runner instead."""
    pidfile = tmp_path / "grandchild.pid"
    outcome = _fast().run_process(
        ProcessRequest(
            command=f"sleep 30 & echo $! > {pidfile}; wait",
            timeout_seconds=0.5,
        )
    )
    grandchild = _read_pid(pidfile)
    try:
        assert outcome.timed_out is True
        assert _wait_gone(grandchild), "the grandchild outlived the operation"
        assert outcome.termination.group_signalled is True
    finally:
        _reap(grandchild)


@posix_only
def test_cancellation_reaches_the_grandchild_too(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        outcome = _fast().run_process(
            ProcessRequest(
                command=f"sleep 30 & echo $! > {pidfile}; wait",
                timeout_seconds=30.0,
            ),
            cancel=cancel,
        )
    finally:
        timer.cancel()
    grandchild = _read_pid(pidfile)
    try:
        assert outcome.cancelled is True
        assert _wait_gone(grandchild), "the grandchild outlived a cancellation"
    finally:
        _reap(grandchild)


@posix_only
def test_a_deep_descendant_chain_is_reached(tmp_path: Path) -> None:
    """Two levels down, not one — group membership is inherited transitively."""
    pidfile = tmp_path / "deep.pid"
    outcome = _fast().run_process(
        ProcessRequest(
            command=f"sh -c 'sleep 30 & echo $! > {pidfile}; wait' & wait",
            timeout_seconds=0.5,
        )
    )
    deep = _read_pid(pidfile)
    try:
        assert outcome.timed_out is True
        assert _wait_gone(deep), "a great-grandchild outlived the operation"
    finally:
        _reap(deep)


@posix_only
def test_the_command_actually_leads_its_own_process_group() -> None:
    """The mechanism itself, observed rather than inferred from behaviour."""
    outcome = HostRunner().run_process(
        ProcessRequest(argv=(sys.executable, "-c", "import os; print(os.getpid(), os.getpgid(0))"))
    )
    pid, pgid = (int(part) for part in outcome.stdout.split())
    assert pid == pgid, "the command should lead its own group"
    assert pgid != os.getpgid(0), "and that group must not be this test runner's"


# --- honest gaps ------------------------------------------------------------


@posix_only
def test_a_descendant_that_leaves_the_group_survives_and_output_is_marked_partial(
    tmp_path: Path,
) -> None:
    """setsid defeats the process group, and the outcome must not hide it.

    This is the documented POSIX limit made executable. The escaped process both
    survives termination and keeps the stdout pipe open, so the readers never
    reach EOF and the captured stream is a prefix. Reporting that honestly is the
    whole point: a runner that returned the prefix as the complete output would
    be lying about what the command produced.
    """
    pidfile = tmp_path / "escaped.pid"
    escape = (
        "import os, sys, time; os.setsid(); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(3)"
    )
    outcome = _fast().run_process(
        ProcessRequest(
            command=f"{sys.executable} -c {escape!r} & sleep 30",
            timeout_seconds=0.5,
        )
    )
    escaped = _read_pid(pidfile)
    try:
        assert outcome.timed_out is True
        assert _alive(escaped), "a setsid'd descendant is out of the group's reach"
        assert outcome.output_complete is False, (
            "the readers were still blocked on a pipe the survivor holds open; "
            "the captured streams are a prefix and must be reported as one"
        )
    finally:
        _reap(escaped)


def test_output_complete_is_true_for_an_ordinary_command() -> None:
    assert HostRunner().run_process(ProcessRequest(command="echo hi")).output_complete is True


# --- per-platform honesty ---------------------------------------------------


def test_the_platform_limit_is_published_as_data_not_only_as_prose() -> None:
    """A consumer must not have to match on sys.platform to learn the limit."""
    described = HostRunner().describe()
    assert described["orphan_prevention"] in ORPHAN_PREVENTION
    assert described["orphan_prevention_note"] == ORPHAN_PREVENTION[described["orphan_prevention"]]
    assert described["process_groups"] == process_group_supported()
    assert described["platform"] == sys.platform


def test_every_outcome_carries_the_platform_limit() -> None:
    payload = HostRunner().run_process(ProcessRequest(command="true")).to_dict()
    termination = payload["termination"]
    assert termination["orphan_prevention"] in ORPHAN_PREVENTION
    assert termination["orphan_prevention_note"]


def test_the_windows_limit_names_what_is_not_terminated() -> None:
    """The gap is stated, not implied. Named here so a reword cannot quietly drop it."""
    note = ORPHAN_PREVENTION["direct-child-only"]
    assert "Grandchildren are NOT" in note
    assert "outlive" in note


def test_the_posix_note_does_not_claim_total_orphan_prevention() -> None:
    note = ORPHAN_PREVENTION["process-group"]
    assert "setsid" in note
    assert "rather than eliminating" in note


def test_the_module_docstring_names_windows_explicitly() -> None:
    """Acceptance criterion: platforms with incomplete prevention are NAMED."""
    from shell.runners import host

    doc = host.__doc__ or ""
    assert "Windows" in doc
    assert "direct child only" in doc
    assert "POSIX" in doc


def test_the_posture_is_still_a_guard_not_a_sandbox() -> None:
    """Adding execution must not have upgraded the claim."""
    described = HostRunner().describe()
    assert described["isolation"] == "none"
    assert described["isolation_note"] == HOST_ISOLATION_NOTE
    assert "not a sandbox" in described["isolation_note"].lower()


def test_a_process_group_is_never_described_as_containment() -> None:
    from shell.runners import host

    doc = (host.__doc__ or "").lower()
    assert "cleanup mechanism" in doc
    assert "boundary of" in doc, "the docstring must deny that a group is a boundary"


# --- serialization ----------------------------------------------------------


def test_an_outcome_round_trips_to_json_shaped_data() -> None:
    import json

    payload = HostRunner().run_process(ProcessRequest(command="echo hi")).to_dict()
    assert json.loads(json.dumps(payload))["stdout"] == "hi\n"


def test_duration_is_derived_from_the_recorded_bounds() -> None:
    outcome = HostRunner().run_process(ProcessRequest(command="true"))
    assert outcome.ended_at >= outcome.started_at
    assert outcome.duration_ms == pytest.approx((outcome.ended_at - outcome.started_at) * 1000.0)


def test_a_default_outcome_reports_no_termination() -> None:
    outcome = ProcessOutcome(exit_code=0)
    assert outcome.termination.terminated is False
    assert outcome.to_dict()["termination"]["steps"] == []
