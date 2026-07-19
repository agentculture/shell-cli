"""The harness protocol itself is provider-neutral (t74a AC3).

Runs unconditionally -- no colleague checkout needed. The
:class:`FakeToolProvider` used here is a throwaway test double (see its
module docstring), not a preview of ``shell.operations``; the point of this
file is purely structural: prove that ``ToolCall``/``ToolCallResult`` do not
name colleague anywhere, and that satisfying :class:`ToolProvider` requires
nothing but a matching ``call`` method.
"""

from __future__ import annotations

from tests.characterization.fake_provider import FakeToolProvider
from tests.characterization.harness import ToolCall, ToolCallResult, ToolProvider


def test_tool_call_carries_a_name_and_arguments():
    call = ToolCall(name="read_file", arguments={"path": "x.txt"})
    assert call.name == "read_file"
    assert call.arguments == {"path": "x.txt"}


def test_tool_call_arguments_default_to_an_empty_dict():
    assert ToolCall(name="list_dir").arguments == {}


def test_tool_call_result_defaults_to_a_bare_ok_flag():
    result = ToolCallResult(ok=True)
    assert result.result is None
    assert result.error is None
    assert result.error_type is None
    assert result.changed_file is None
    assert result.bytes_written is None
    assert result.media_part is None


def test_fake_provider_satisfies_the_protocol():
    provider: ToolProvider = FakeToolProvider()
    result = provider.call(ToolCall(name="read_file", arguments={"path": "x"}))
    assert isinstance(result, ToolCallResult)
    assert result.ok is True


def test_protocol_membership_is_structural_not_nominal():
    """Any object with a matching call() method satisfies ToolProvider --
    no shared base class required. This is what lets a future
    shell.operations adapter plug into the same characterization suite
    without this harness importing it."""

    class AdHocProvider:
        def call(self, tool_call: ToolCall) -> ToolCallResult:
            return ToolCallResult(ok=True, result=f"adhoc:{tool_call.name}")

    provider: ToolProvider = AdHocProvider()
    assert provider.call(ToolCall(name="write_file")).result == "adhoc:write_file"


def test_harness_module_names_no_concrete_engine():
    """harness.py must stay import-clean of both colleague and any future
    shell.operations module -- it is the seam, not an implementation.

    Checks actual `import`/`from ... import` statements (via ast), not a
    raw substring search -- the module's own docstring explains this
    boundary in prose and mentions both names, which a substring check
    would misfire on.
    """
    import ast
    from pathlib import Path

    import tests.characterization.harness as harness_module

    text = Path(harness_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert "colleague" not in imported_roots
    assert "shell" not in imported_roots
