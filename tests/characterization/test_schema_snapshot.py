"""Byte-for-byte pin on the six colleague-compatible tool schemas (t74a AC2).

``SCHEMAS[:6]`` in ``colleague/tools.py`` (read_file, view_media, write_file,
edit_file, list_dir, run_command; the 7th entry, ``culture``, is not part of
this slice) is generated — never hand-written — into
``tests/fixtures/colleague/schemas.json`` by
``scripts/capture_colleague_baseline.py``. Every trap CLAUDE.md names is
asserted explicitly here, not just implied by a byte-equality check, so a
regression names what broke instead of just showing a diff.
"""

from __future__ import annotations

import json

from tests.characterization.fixtures import FIXTURES_DIR, load_schemas

_SIX_NAMES = [
    "read_file",
    "view_media",
    "write_file",
    "edit_file",
    "list_dir",
    "run_command",
]


def test_schemas_fixture_is_committed():
    assert (FIXTURES_DIR / "schemas.json").is_file()


def test_exactly_the_six_compatibility_schemas_in_order():
    """SCHEMAS[:6] is a contiguous slice -- order is part of the contract."""
    schemas = load_schemas()
    assert [s["function"]["name"] for s in schemas] == _SIX_NAMES


def test_every_schema_is_an_openai_function_tool():
    for schema in load_schemas():
        assert schema["type"] == "function"
        assert set(schema.keys()) == {"type", "function"}


def test_list_dir_uniquely_has_no_required_key():
    """list_dir has no `required` key at all (not an empty list) -- do not
    normalize one in; that would change what an engine treats as mandatory."""
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    assert "required" not in schemas["list_dir"]["function"]["parameters"]


def test_every_other_schema_declares_required():
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    for name in _SIX_NAMES:
        if name == "list_dir":
            continue
        assert "required" in schemas[name]["function"]["parameters"], name


def test_path_desc_is_interpolated_verbatim_into_four_schemas():
    """_PATH_DESC (colleague/tools.py:61) is shared across read_file,
    view_media, write_file, and edit_file -- list_dir and run_command each
    use their own description instead, so they must NOT match it."""
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    path_desc = schemas["read_file"]["function"]["parameters"]["properties"]["path"]["description"]
    assert path_desc == "Path relative to the repo root."
    for name in ("view_media", "write_file", "edit_file"):
        assert (
            schemas[name]["function"]["parameters"]["properties"]["path"]["description"]
            == path_desc
        ), name
    assert (
        schemas["list_dir"]["function"]["parameters"]["properties"]["path"]["description"]
        != path_desc
    )


def test_read_file_description_embeds_literal_backslash_escapes():
    """The description text says "cat -n style, e.g. '    12\\tsome code'" --
    a literal backslash-t (two characters), not an actual tab."""
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    desc = schemas["read_file"]["function"]["description"]
    assert "\\t" in desc
    assert "\t" not in desc


def test_write_file_requires_path_and_content():
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    assert schemas["write_file"]["function"]["parameters"]["required"] == ["path", "content"]


def test_edit_file_requires_path_old_string_new_string():
    schemas = {s["function"]["name"]: s for s in load_schemas()}
    assert schemas["edit_file"]["function"]["parameters"]["required"] == [
        "path",
        "old_string",
        "new_string",
    ]


def test_schemas_json_round_trips_under_the_generator_serialization():
    """The committed file's bytes must be exactly what the generator's fixed
    json.dumps(..., indent=2, ensure_ascii=False) + newline shape produces --
    the whitespace and escaping are part of what "byte-for-byte" pins."""
    text = (FIXTURES_DIR / "schemas.json").read_text(encoding="utf-8")
    reserialized = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
    assert text == reserialized
