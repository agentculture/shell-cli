"""Fixture regeneration is proven byte-identical (t74a AC4).

CI has no colleague checkout (the same constraint documented in
``tests/test_colleague_inventory.py``), so every test here is skipped when
one is not available at ``colleague_adapter.DEFAULT_COLLEAGUE_ROOT`` (or
``$SHELL_CLI_COLLEAGUE_ROOT``). They run for real in this task's own
environment, which is the actual proof the acceptance criterion requires:
re-running the generator against the same commit reproduces byte-identical
fixtures.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.characterization.colleague_adapter import DEFAULT_COLLEAGUE_ROOT, colleague_available
from tests.characterization.fixtures import FIXTURES_DIR

_GENERATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "capture_colleague_baseline.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("capture_colleague_baseline", _GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()

pytestmark = pytest.mark.skipif(
    not colleague_available(),
    reason=(
        f"no colleague checkout at {DEFAULT_COLLEAGUE_ROOT} "
        "(set SHELL_CLI_COLLEAGUE_ROOT to point elsewhere) -- CI does not have one, "
        "see tests/test_colleague_inventory.py for the same constraint"
    ),
)


def test_pinned_sha_matches_the_observed_checkout():
    """The whole baseline is only honest if it was captured at the pinned
    commit -- if this fails, the checkout has moved and the committed
    fixtures no longer describe what is on disk."""
    observed = generator._head_sha(DEFAULT_COLLEAGUE_ROOT)
    assert observed == generator.PINNED_SHA
    assert generator.PINNED_VERSION == "1.51.0"


def test_regeneration_matches_the_committed_fixtures_byte_for_byte(tmp_path):
    written = generator.generate(DEFAULT_COLLEAGUE_ROOT, tmp_path)

    assert set(written) == {
        generator.SCHEMAS_FILENAME,
        generator.BEHAVIOR_FILENAME,
        generator.META_FILENAME,
    }
    for filename, path in written.items():
        committed = FIXTURES_DIR / filename
        assert path.read_bytes() == committed.read_bytes(), (
            f"{filename} regenerated differently from the committed fixture -- "
            "either the checkout drifted off the pinned SHA, or the generator "
            "is not deterministic"
        )


def test_two_independent_regenerations_are_byte_identical(tmp_path):
    """The generator itself, run twice, must not depend on anything that
    varies between runs (wall-clock time, random temp-dir names, PID, ...)."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    written_a = generator.generate(DEFAULT_COLLEAGUE_ROOT, out_a)
    written_b = generator.generate(DEFAULT_COLLEAGUE_ROOT, out_b)

    assert set(written_a) == set(written_b)
    for filename in written_a:
        assert written_a[filename].read_bytes() == written_b[filename].read_bytes(), filename
