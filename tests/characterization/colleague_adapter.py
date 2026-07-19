"""Binds the provider-neutral :class:`ToolProvider` protocol to LIVE colleague.

This is the proof half of t74a's acceptance criterion 3: the SAME
``ToolCall``/``ToolCallResult`` shape a future ``shell.operations`` adapter
will use can drive real colleague code today. ``ColleagueToolProvider`` is
that adapter — nothing else in this package (the harness protocol, the
fixtures loader) knows this module exists.

CI has no colleague checkout (the same constraint
``tests/test_colleague_inventory.py`` documents for the sibling inventory
scanner), so every test that instantiates :class:`ColleagueToolProvider`
must guard on :func:`colleague_available` first and skip otherwise — never
assume the checkout is there.

Every call runs in colleague's OWN interpreter, exactly like
``scripts/capture_colleague_baseline.py`` — colleague pins one third-party
dependency (``agentfront``) that shell-cli's own process must never import
(the zero-deps guard). The venv/``uv run`` detection logic is not
duplicated here: it is loaded BY PATH from the generator script, the same
``importlib.util.spec_from_file_location`` trick
``tests/test_colleague_inventory.py`` already uses to load
``scripts/colleague_inventory.py`` (``scripts/`` is deliberately not a
package).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess  # nosec B404 - fixed argv vectors only, no shell strings
import sys
import tempfile
from pathlib import Path
from typing import Any

from tests.characterization.harness import ToolCall, ToolCallResult

_GENERATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "capture_colleague_baseline.py"

#: Overridable so a different checkout location doesn't require editing code.
DEFAULT_COLLEAGUE_ROOT = Path(
    os.environ.get("SHELL_CLI_COLLEAGUE_ROOT", "/home/spark/git/colleague")
)

_CALL_TIMEOUT_SECONDS = 60

# The program that runs INSIDE colleague's own interpreter for exactly ONE
# tool call. Deliberately re-creates a fresh ToolExecutor per call (never
# reused across calls) so `bytes_written` and `changed` reflect only this
# call, matching how scripts/capture_colleague_baseline.py captured the
# fixtures this adapter is compared against.
_CALL_CHILD_SOURCE = r"""
import json
import sys
from pathlib import Path

colleague_root, workspace, name, arguments_json = sys.argv[1:5]
sys.path.insert(0, colleague_root)
from colleague.tools import ToolExecutor  # noqa: E402

arguments = json.loads(arguments_json)
executor = ToolExecutor(Path(workspace))
try:
    outcome = executor.execute(name, arguments)
    payload = {
        "ok": True,
        "result": outcome.result,
        "changed_file": outcome.changed_file,
        "media_part": outcome.media_part,
        "bytes_written": executor.bytes_written,
    }
except Exception as exc:  # noqa: BLE001 - captured as data, mirrors execute()'s own wrap
    payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}

sys.stdout.write(json.dumps(payload))
"""


def _load_generator():
    """Import scripts/capture_colleague_baseline.py by path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("capture_colleague_baseline", _GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_generator = _load_generator()


def colleague_available(root: Path = DEFAULT_COLLEAGUE_ROOT) -> bool:
    """True when *root* looks like a colleague checkout this adapter can drive."""
    return (root / "colleague").is_dir()


class ColleagueToolProvider:
    """A :class:`ToolProvider` bound to one live colleague checkout + workspace.

    Each :meth:`call` spawns a fresh subprocess in colleague's own
    interpreter against a FRESH ``ToolExecutor`` rooted at ``self.workspace``
    — callers control that workspace's contents directly (write fixture
    files into it before calling), the same way
    ``scripts/capture_colleague_baseline.py``'s capture child does.
    """

    def __init__(
        self,
        colleague_root: Path = DEFAULT_COLLEAGUE_ROOT,
        *,
        workspace: Path | None = None,
    ) -> None:
        self._colleague_root = colleague_root
        self._workspace = workspace or Path(tempfile.mkdtemp(prefix="shell_cli_characterization_"))
        self._workspace.mkdir(parents=True, exist_ok=True)

    @property
    def workspace(self) -> Path:
        return self._workspace

    def call(self, tool_call: ToolCall) -> ToolCallResult:
        interpreter = _generator.colleague_interpreter(self._colleague_root)
        fd, script_name = tempfile.mkstemp(suffix=".py")
        script_path = Path(script_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_CALL_CHILD_SOURCE)
            argv = interpreter + [
                str(script_path),
                str(self._colleague_root),
                str(self._workspace),
                tool_call.name,
                json.dumps(tool_call.arguments),
            ]
            proc = subprocess.run(  # nosec B603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=_CALL_TIMEOUT_SECONDS,
                check=False,
            )
        finally:
            script_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            return ToolCallResult(
                ok=False,
                error_type="AdapterError",
                error=proc.stderr.strip() or f"child exited {proc.returncode}",
            )

        data: dict[str, Any] = json.loads(proc.stdout)
        return ToolCallResult(
            ok=data["ok"],
            result=data.get("result"),
            error=data.get("error"),
            error_type=data.get("error_type"),
            changed_file=data.get("changed_file"),
            bytes_written=data.get("bytes_written"),
            media_part=data.get("media_part"),
        )
