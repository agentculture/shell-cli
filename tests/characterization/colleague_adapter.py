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

import functools
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
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _true_checkout_root() -> Path:
    """The directory a SIBLING checkout of colleague would actually live in.

    For an ordinary clone this is just ``_REPO_ROOT`` itself. This task
    frequently runs inside a LINKED git worktree (e.g.
    ``<repo>/.claude/worktrees/<id>/``), whose own toplevel is a nested
    sandbox path, not the original checkout -- sibling discovery below would
    otherwise look for colleague next to ``.claude/worktrees/`` instead of
    next to the real repo. ``git rev-parse --git-common-dir`` always resolves
    to the ORIGINAL repository's ``.git``, regardless of which linked
    worktree asks, so its parent is the right anchor either way. Falls back
    to ``_REPO_ROOT`` on any failure (no git on PATH, not a repo, ...) --
    discovery failing just means the guess below resolves to a path that
    does not exist, which :func:`colleague_available` already turns into a
    clean skip, never a failure.
    """
    try:
        proc = subprocess.run(  # nosec B603,B607 - fixed argv, no shell, git from PATH
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve().parent
    except (OSError, subprocess.SubprocessError):
        pass
    return _REPO_ROOT


def _discover_colleague_root() -> Path:
    """Best-effort default -- never a hardcoded personal path.

    Priority: ``$SHELL_CLI_COLLEAGUE_ROOT``, then a ``colleague/`` checkout
    cloned as a SIBLING of this repo's real checkout (``<workspace>/shell-cli``
    next to ``<workspace>/colleague`` -- the layout the wider multi-project
    workspace this repo lives in already uses). Neither branch is assumed to
    exist: :func:`colleague_available` verifies actual drivability below, so
    a wrong guess here only ever produces a skip, never a failure, on
    someone else's machine.
    """
    env_value = os.environ.get("SHELL_CLI_COLLEAGUE_ROOT")
    if env_value:
        return Path(env_value)
    return _true_checkout_root().parent / "colleague"


#: Overridable via $SHELL_CLI_COLLEAGUE_ROOT; otherwise a sibling-checkout
#: guess (see _discover_colleague_root). Never load-bearing for a pass/fail
#: outcome -- colleague_available() verifies real drivability, so a wrong
#: guess here only ever changes a skip REASON, never turns a skip into a
#: failure.
DEFAULT_COLLEAGUE_ROOT = _discover_colleague_root()

_CALL_TIMEOUT_SECONDS = 60
_AVAILABILITY_PROBE_TIMEOUT_SECONDS = 30

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


def _import_probe_argv(root: Path) -> list[str]:
    """The argv that checks whether ``colleague.tools`` actually imports.

    Reuses :func:`colleague_interpreter`'s own choice (the checkout's
    ``.venv`` when materialized, else the ``uv run --project`` fallback) --
    but when that fallback is what gets picked, inserts ``--no-sync`` so a
    bare, un-synced checkout (a plain ``git clone``, no ``.venv``) fails
    FAST and reports "not drivable" instead of silently installing
    colleague's whole dependency tree (``agentfront`` and friends) as a side
    effect of merely checking availability. That install-on-check behaviour
    is exactly the cost CI's KNOWN GAP note (.github/workflows/tests.yml)
    declined to pay on every push.
    """
    interpreter = _generator.colleague_interpreter(root)
    if interpreter[:2] == ["uv", "run"]:
        interpreter = interpreter[:2] + ["--no-sync"] + interpreter[2:]
    return interpreter + ["-c", "import colleague.tools"]


@functools.lru_cache(maxsize=None)
def _drivability(root_str: str) -> tuple[bool, str]:
    """(is_drivable, reason) for the checkout at *root_str*.

    Cached per root so every skipif-gated module (and every call site that
    only needs the bool) pays at most one subprocess per process, not one
    per test. The two failure branches are kept textually distinguishable
    on purpose -- "checkout not found" and "found but not importable (no
    environment)" describe different problems for someone reading a `-rs`
    skip list to diagnose, and collapsing them back into one generic
    "unavailable" string is the exact defect this function replaces.
    """
    root = Path(root_str)
    if not (root / "colleague").is_dir():
        return False, f"colleague checkout not found at {root}"

    argv = _import_probe_argv(root)
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=_AVAILABILITY_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (
            f"colleague found at {root} but its interpreter could not be launched: {exc}"
        )

    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "import failed"
        return False, f"colleague found at {root} but not importable (no environment): {detail}"

    return True, f"colleague at {root} is importable"


def colleague_available(root: Path = DEFAULT_COLLEAGUE_ROOT) -> bool:
    """True when *root* is not just PRESENT but DRIVABLE.

    "Present" (a ``colleague/`` directory exists) is not enough:
    :class:`ColleagueToolProvider` spawns colleague's OWN interpreter so
    ``agentfront`` resolves, and a bare ``git clone`` (exactly what a CI
    checkout produces) has the directory but no materialized environment.
    Treating that as "available" would let the live tests run and FAIL on a
    plain clone instead of skipping -- a skip is visible and honest; a red
    CI run on a machine that merely lacks an environment sends people
    hunting for a bug that is not there. This actually resolves the
    interpreter the adapter would use and confirms ``colleague.tools``
    imports in it.
    """
    drivable, _ = _drivability(str(root))
    return drivable


def colleague_unavailable_reason(root: Path = DEFAULT_COLLEAGUE_ROOT) -> str:
    """Why :func:`colleague_available` returned False for *root*.

    Distinguishes "colleague checkout not found" from "colleague found but
    not importable (no environment)" -- the two skip reasons callers should
    surface separately in ``pytest.mark.skipif(..., reason=...)`` so a `-rs`
    skip list lets a reader tell "nobody provided colleague" apart from
    "colleague is there but unusable". Returns "" when actually available.
    """
    drivable, reason = _drivability(str(root))
    return "" if drivable else reason


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
