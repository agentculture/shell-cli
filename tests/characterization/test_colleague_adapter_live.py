"""Proves the harness can drive LIVE colleague today (t74a AC3).

Skipped when no colleague checkout is available -- CI does not have one (see
``tests/test_colleague_inventory.py``'s docstring for the same constraint on
the sibling inventory scanner). Runs for real in any environment that does
have the checkout, which is the actual proof this task's acceptance
criterion requires: "The characterization tests should be able to run
against colleague today ... with no change to the fixtures."

Each test below drives :class:`ColleagueToolProvider` through the SAME
``ToolCall``/``ToolCallResult`` shape ``test_harness_protocol.py`` exercises
against a throwaway fake, and checks the live result against the value
``scripts/capture_colleague_baseline.py`` already captured -- proving the
adapter reaches the real handler code, not a stand-in.
"""

from __future__ import annotations

import shutil

import pytest

from tests.characterization.colleague_adapter import (
    DEFAULT_COLLEAGUE_ROOT,
    ColleagueToolProvider,
    colleague_available,
)
from tests.characterization.fixtures import load_behavior
from tests.characterization.harness import ToolCall

pytestmark = pytest.mark.skipif(
    not colleague_available(),
    reason=(
        f"no colleague checkout at {DEFAULT_COLLEAGUE_ROOT} "
        "(set SHELL_CLI_COLLEAGUE_ROOT to point elsewhere)"
    ),
)


@pytest.fixture
def provider(tmp_path):
    p = ColleagueToolProvider(workspace=tmp_path / "ws")
    yield p
    shutil.rmtree(p.workspace, ignore_errors=True)


def test_write_file_round_trip_matches_the_captured_fixture(provider):
    fixture = load_behavior()["behaviors"]["write_file_bytes_written"]

    result = provider.call(
        ToolCall(name="write_file", arguments={"path": "w.txt", "content": "hello world"})
    )

    assert result.ok is True
    assert result.result == fixture["result"]
    assert result.bytes_written == fixture["bytes_written"]
    assert (provider.workspace / "w.txt").read_text(encoding="utf-8") == "hello world"


def test_list_dir_shape_matches_the_captured_fixture(provider):
    fixture = load_behavior()["behaviors"]["list_dir_shape"]
    (provider.workspace / "dirtest").mkdir()
    (provider.workspace / "dirtest" / "b.txt").write_text("x", encoding="utf-8")
    (provider.workspace / "dirtest" / "a.txt").write_text("x", encoding="utf-8")
    (provider.workspace / "dirtest" / "sub").mkdir()

    result = provider.call(ToolCall(name="list_dir", arguments={"path": "dirtest"}))

    assert result.ok is True
    assert result.result == fixture["result"]


def test_read_file_numbering_order_matches_the_captured_fixture(provider):
    """The single strongest live check: the load-bearing #240 ordering fix,
    reproduced against real colleague right now, not just replayed from a
    stored string."""
    fixture = load_behavior()["behaviors"]["read_file_numbers_then_truncates"]
    (provider.workspace / "big.txt").write_text(fixture["input_text"], encoding="utf-8")

    result = provider.call(ToolCall(name="read_file", arguments={"path": "big.txt"}))

    assert result.ok is True
    assert result.result == fixture["result"]


def test_execute_still_wraps_a_non_tool_error_live(provider):
    fixture = load_behavior()["behaviors"]["execute_wraps_non_tool_error"]

    result = provider.call(ToolCall(name="read_file", arguments={"path": "a\x00b"}))

    assert result.ok is False
    assert result.error_type == fixture["error_type"] == "ToolError"
    assert result.error == fixture["error"]


def test_unknown_tool_is_still_a_distinct_error_type_live(provider):
    result = provider.call(ToolCall(name="no_such_tool"))

    assert result.ok is False
    assert result.error_type == "UnknownToolError"
