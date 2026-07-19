"""Loads the generated colleague baseline fixtures (tests/fixtures/colleague/).

Every value returned here was produced by
``scripts/capture_colleague_baseline.py`` against a live colleague checkout
at the pinned SHA — never hand-written. This module only reads the committed
JSON; it has no opinion about which provider (colleague today, shell-cli
tomorrow) a characterization test is driving.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "colleague"

SCHEMAS_PATH = FIXTURES_DIR / "schemas.json"
BEHAVIOR_PATH = FIXTURES_DIR / "behavior.json"
META_PATH = FIXTURES_DIR / "meta.json"


def load_schemas() -> list[dict[str, Any]]:
    """The six colleague-compatible tool schemas, verbatim (``SCHEMAS[:6]``)."""
    return json.loads(SCHEMAS_PATH.read_text(encoding="utf-8"))


def load_behavior() -> dict[str, Any]:
    """``{"constants": {...}, "behaviors": {<label>: <captured outcome>}}``."""
    return json.loads(BEHAVIOR_PATH.read_text(encoding="utf-8"))


def load_meta() -> dict[str, Any]:
    """``{"pinned_sha", "pinned_version", "observed_sha", "sha_matches"}``."""
    return json.loads(META_PATH.read_text(encoding="utf-8"))
