"""A minimal, deliberately trivial second :class:`ToolProvider`.

This is a TEST DOUBLE, not a preview of shell-cli's future
``shell.operations`` engine — that engine does not exist yet and this task
must not create it (see CLAUDE.md's Milestone 1 boundary and the t74a task
brief). Its only job is to prove the harness protocol in ``harness.py`` is
genuinely provider-neutral: the exact same ``ToolCall``/``ToolCallResult``
calling convention that drives :class:`ColleagueToolProvider` (a real
engine, colleague, bound over a subprocess) also drives this one (no engine
at all, just a stdlib dict), with no branching in a caller and no change to
``tests/fixtures/colleague/``.

It deliberately does NOT try to reproduce colleague's exact result strings
or error wording — encoding colleague's private phrasing as "the" contract
here would blur this task's boundary with t87 (Milestone 2 differential
parity), which compares two real implementations and is explicitly out of
scope for t74a (see the task's SCOPE note: "capturing a target, not
comparing against one").
"""

from __future__ import annotations

from tests.characterization.harness import ToolCall, ToolCallResult


class FakeToolProvider:
    """Answers every call with a fixed, recognisably-fake, non-colleague shape."""

    def call(self, tool_call: ToolCall) -> ToolCallResult:
        return ToolCallResult(ok=True, result=f"fake:{tool_call.name}", bytes_written=0)
