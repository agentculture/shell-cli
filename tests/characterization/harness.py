"""A provider-neutral characterization harness (shell-cli task t74a).

Milestone 0 needs to prove the six colleague-compatible tool schemas and
their behavioural contract can be characterized WITHOUT wiring the
characterization suite to any one engine. ``shell.operations`` does not
exist yet and must not be created here (that is Milestone 1's job) — so this
module defines the seam a characterization test drives as a plain
:class:`typing.Protocol`, never importing colleague or any future shell-cli
runtime type.

The dataclasses below are deliberately the smallest common shape that both
colleague's ``ToolOutcome``/``ToolError`` (today) and shell-cli's eventual
``OperationResult`` (tomorrow) can be adapted to, without either importing
the other. An adapter module (see ``colleague_adapter.py`` in this package)
binds ONE concrete engine to this protocol; the characterization tests and
the fixtures under ``tests/fixtures/colleague/`` depend on nothing but the
protocol itself, so pointing the same suite at a different engine is a
one-module change, not a fixture rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """One characterization step: a tool name plus its normalized arguments.

    Mirrors the shape every one of the six compatibility tools accepts today
    (``name`` + a JSON-object ``arguments`` dict) without naming colleague's
    ``ToolCall`` type or any future ``shell.operations.Operation`` type.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResult:
    """The provider-neutral outcome of one :class:`ToolCall`.

    Exactly one of ``result`` / ``error`` is meaningful, selected by ``ok`` —
    this is intentionally coarser than colleague's ``ToolOutcome`` (which
    only ever carries a success value; a failure instead raises
    ``ToolError``/``UnknownToolError``) and coarser than whatever shape a
    future ``OperationResult`` settles on. An adapter folds either its
    engine's exception or its engine's success value into this one shape, so
    a characterization assertion never needs to know which engine is behind
    the call.

    ``error_type`` carries just enough of the engine's own exception
    taxonomy to distinguish, e.g., a plain recoverable tool error from an
    "unknown tool" protocol error — colleague's ``ToolError`` vs
    ``UnknownToolError`` — without importing either class.
    """

    ok: bool
    result: str | None = None
    error: str | None = None
    error_type: str | None = None
    changed_file: str | None = None
    bytes_written: int | None = None
    media_part: dict[str, Any] | None = None


class ToolProvider(Protocol):
    """The seam a characterization test drives.

    Structural (a :class:`typing.Protocol`), not nominal: any object with a
    matching ``call`` method satisfies this — no shared base class, and
    therefore no import coupling between an adapter for colleague today and
    an adapter for ``shell.operations`` tomorrow.
    """

    def call(self, tool_call: ToolCall) -> ToolCallResult:
        """Execute *tool_call* against this provider's engine and workspace."""
        ...
