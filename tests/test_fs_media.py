"""Tests for fs.media — confined media observation.

Tests that the vendored media handler:
* loads valid images as base64-encoded parts;
* refuses oversized images with the handler-level 4 MiB cap;
* refuses audio and other non-image media with the images-only rule;
* refuses unknown extensions;
* behavior matches colleague's view_media on the same inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell.environment import Environment, WorkspaceKind
from shell.operations import Operation, OperationIntent, handler_for, registered_kinds
from shell.results import OperationStatus
from shell.runners.host import HostRunner

# Importing the module is what registers the ``fs.media`` handler. Relying on
# another test module to have imported it first passes serially and fails under
# ``pytest -n auto``, where a worker may run only this file.
import shell.fs.media  # noqa: E402,F401  isort:skip


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    """A temporary workspace for media tests."""
    source = tmp_path / "checkout"
    work = tmp_path / "worktree"
    source.mkdir()
    work.mkdir()
    return Environment(
        source_root=source,
        work_root=work,
        runner=HostRunner(),
        workspace=WorkspaceKind.WORKTREE,
    )


@pytest.fixture
def png_108_bytes(tmp_path: Path) -> bytes:
    """The actual 108-byte test PNG from colleague's fixtures."""
    # Decode from the behavior.json fixture's data URL.
    # noqa: E501
    data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgowMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"  # noqa: E501
    )
    # Strip the data: prefix and decode base64.
    import base64

    b64_part = data_url.split(",", 1)[1]
    return base64.b64decode(b64_part)


class TestMediaValidation:
    """Tests for the validate_attachment and build_part helpers."""

    def test_validate_attachment_valid_png(self, tmp_path: Path) -> None:
        """A valid PNG passes validate_attachment."""
        from shell.fs.media import validate_attachment

        png_file = tmp_path / "test.png"
        png_file.write_bytes(b"PNG fake data")

        result = validate_attachment(str(png_file))
        assert result["path"] == str(png_file)
        assert result["media_type"] == "image/png"

    def test_validate_attachment_missing_file(self) -> None:
        """Missing file raises ValueError."""
        from shell.fs.media import validate_attachment

        with pytest.raises(ValueError, match="Attachment file not found"):
            validate_attachment("/nonexistent/file.png")

    def test_validate_attachment_directory(self, tmp_path: Path) -> None:
        """A directory raises ValueError."""
        from shell.fs.media import validate_attachment

        with pytest.raises(ValueError, match="not a regular file"):
            validate_attachment(str(tmp_path))

    def test_validate_attachment_unknown_extension(self, tmp_path: Path) -> None:
        """Unknown extension raises ValueError."""
        from shell.fs.media import validate_attachment

        unknown = tmp_path / "file.xyz"
        unknown.write_bytes(b"data")

        with pytest.raises(ValueError, match="Unknown attachment extension 'xyz'"):
            validate_attachment(str(unknown))

    def test_build_part_png(self, tmp_path: Path) -> None:
        """build_part for PNG yields image_url part."""
        from shell.fs.media import build_part, validate_attachment

        png_file = tmp_path / "img.png"
        png_file.write_bytes(b"PNG")

        attachment = validate_attachment(str(png_file))
        part = build_part(attachment)

        assert part["type"] == "image_url"
        assert "image_url" in part
        assert "url" in part["image_url"]
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_build_part_audio(self, tmp_path: Path) -> None:
        """build_part for WAV yields input_audio part (even if handler rejects it)."""
        from shell.fs.media import build_part, validate_attachment

        wav_file = tmp_path / "snd.wav"
        wav_file.write_bytes(b"RIFF fake WAV")

        attachment = validate_attachment(str(wav_file))
        part = build_part(attachment)

        assert part["type"] == "input_audio"
        assert "input_audio" in part
        assert "data" in part["input_audio"]
        assert part["input_audio"]["format"] == "wav"


class TestMediaHandler:
    """Tests for the fs.media handler."""

    def test_media_handler_valid_image(
        self, env: Environment, tmp_path: Path, png_108_bytes: bytes
    ) -> None:
        """A valid 108-byte PNG loads successfully."""
        from shell.fs.media import read_media

        img_file = env.work_root / "img.png"
        img_file.write_bytes(png_108_bytes)

        op = Operation(kind="fs.media", arguments={"path": "img.png"})
        result = read_media(op, env)

        assert result.status == OperationStatus.SUCCEEDED
        assert result.succeeded
        assert "loaded image img.png (108 bytes)" in result.rendering
        assert "media_part" in result.output
        assert result.output["media_part"]["type"] == "image_url"

    def test_media_handler_image_at_size_cap(self, env: Environment) -> None:
        """An image at exactly the 4 MiB cap loads successfully."""
        from shell.fs.media import MAX_MEDIA_BYTES, read_media

        cap_size = MAX_MEDIA_BYTES
        img_file = env.work_root / "at_cap.png"
        img_file.write_bytes(b"PNG" + b"x" * (cap_size - 3))

        op = Operation(kind="fs.media", arguments={"path": "at_cap.png"})
        result = read_media(op, env)

        assert result.status == OperationStatus.SUCCEEDED
        assert result.succeeded

    def test_media_handler_oversize_image(self, env: Environment) -> None:
        """A 4 MiB + 1 byte PNG is refused with the size cap message."""
        from shell.fs.media import MAX_MEDIA_BYTES, read_media

        oversize = MAX_MEDIA_BYTES + 1
        img_file = env.work_root / "big.png"
        img_file.write_bytes(b"PNG" + b"x" * (oversize - 3))

        op = Operation(kind="fs.media", arguments={"path": "big.png"})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        assert f"{oversize} bytes exceeds the {MAX_MEDIA_BYTES}-byte" in result.error
        # Verify the exact message matches colleague's fixture.
        expected = (
            f"cannot view big.png: {oversize} bytes exceeds the "
            f"{MAX_MEDIA_BYTES}-byte media size cap"
        )
        assert result.error == expected

    def test_media_handler_audio_rejected(self, env: Environment) -> None:
        """An audio file is rejected with the images-only message."""
        from shell.fs.media import read_media

        wav_file = env.work_root / "snd.wav"
        wav_file.write_bytes(b"RIFF" + b"x" * 1000)

        op = Operation(kind="fs.media", arguments={"path": "snd.wav"})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        # Verify the exact message matches colleague's fixture.
        expected = "view_media is images only: snd.wav is audio/wav"
        assert result.error == expected

    def test_media_handler_unknown_extension(self, env: Environment) -> None:
        """An unknown extension is rejected."""
        from shell.fs.media import read_media

        unknown_file = env.work_root / "file.xyz"
        unknown_file.write_bytes(b"data")

        op = Operation(kind="fs.media", arguments={"path": "file.xyz"})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        assert "Unknown attachment extension 'xyz'" in result.error

    def test_media_handler_missing_file(self, env: Environment) -> None:
        """A missing file is refused."""
        from shell.fs.media import read_media

        op = Operation(kind="fs.media", arguments={"path": "nonexistent.png"})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        assert "no such file" in result.error

    def test_media_handler_missing_path_argument(self, env: Environment) -> None:
        """Missing path argument is a handler error."""
        from shell.fs.media import read_media

        op = Operation(kind="fs.media", arguments={})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        assert "requires 'path'" in result.error

    def test_media_handler_path_escape(self, env: Environment, tmp_path: Path) -> None:
        """A path escaping the work root is refused."""
        from shell.fs.media import read_media

        outside = tmp_path / "outside.png"
        outside.write_bytes(b"PNG fake")

        # Try to reference it via escape.
        op = Operation(kind="fs.media", arguments={"path": "../outside.png"})
        result = read_media(op, env)

        assert result.status == OperationStatus.FAILED
        assert not result.succeeded
        assert "escapes" in result.error


class TestMediaComparisonWithColleague:
    """Tests that verify shell-cli's behavior matches colleague's fixtures."""

    def test_view_media_ok_matches_fixture(self, env: Environment, png_108_bytes: bytes) -> None:
        """The ok case matches colleague's behavior.json fixture."""
        from shell.fs.media import read_media

        img_file = env.work_root / "img.png"
        img_file.write_bytes(png_108_bytes)

        op = Operation(kind="fs.media", arguments={"path": "img.png"})
        result = read_media(op, env)

        # Verify against the fixture.
        assert result.succeeded
        assert result.output["result"] == "loaded image img.png (108 bytes) into the conversation"
        assert result.output["media_part"]["type"] == "image_url"
        assert "image_url" in result.output["media_part"]
        assert "url" in result.output["media_part"]["image_url"]

    def test_view_media_audio_rejected_matches_fixture(self, env: Environment) -> None:
        """The audio rejection matches colleague's fixture exactly."""
        from shell.fs.media import read_media

        wav_file = env.work_root / "snd.wav"
        wav_file.write_bytes(b"RIFF" + b"x" * 1000)

        op = Operation(kind="fs.media", arguments={"path": "snd.wav"})
        result = read_media(op, env)

        # Verify against the fixture: "view_media is images only: snd.wav is audio/wav"
        assert not result.succeeded
        assert result.error == "view_media is images only: snd.wav is audio/wav"

    def test_view_media_oversize_rejected_matches_fixture(self, env: Environment) -> None:
        """The oversize rejection matches colleague's fixture exactly."""
        from shell.fs.media import MAX_MEDIA_BYTES, read_media

        oversize = MAX_MEDIA_BYTES + 1
        img_file = env.work_root / "big.png"
        img_file.write_bytes(b"PNG" + b"x" * (oversize - 3))

        op = Operation(kind="fs.media", arguments={"path": "big.png"})
        result = read_media(op, env)

        # Verify against the fixture.
        assert not result.succeeded
        expected = (
            f"cannot view big.png: {oversize} bytes exceeds the "
            f"{MAX_MEDIA_BYTES}-byte media size cap"
        )
        assert result.error == expected


class TestMediaVendorBoundary:
    """Tests that verify the vendoring boundary is maintained."""

    def test_flatten_parts_not_vendored(self) -> None:
        """flatten_parts is NOT in shell.fs.media (remains in colleague)."""
        import shell.fs.media as media_module

        assert not hasattr(media_module, "flatten_parts")

    def test_image_token_estimate_not_vendored(self) -> None:
        """IMAGE_TOKEN_ESTIMATE is NOT in shell.fs.media (remains in colleague)."""
        import shell.fs.media as media_module

        assert not hasattr(media_module, "IMAGE_TOKEN_ESTIMATE")

    def test_flatten_parts_not_defined(self) -> None:
        """flatten_parts function is not defined in shell.fs.media."""
        import inspect

        members = dict(inspect.getmembers(shell.fs.media))
        assert "flatten_parts" not in members

    def test_image_token_estimate_not_defined(self) -> None:
        """IMAGE_TOKEN_ESTIMATE constant is not defined in shell.fs.media."""
        import inspect

        members = dict(inspect.getmembers(shell.fs.media))
        assert "IMAGE_TOKEN_ESTIMATE" not in members


class TestMediaHandlerRegistration:
    """Tests that fs.media is registered correctly."""

    def test_media_kind_is_registered(self) -> None:
        """fs.media is registered as an operation kind."""
        registered = registered_kinds()
        assert "fs.media" in registered

    def test_media_kind_intent(self) -> None:
        """fs.media has intent OBSERVE."""
        handler = handler_for("fs.media")
        assert handler.intent == OperationIntent.OBSERVE

    def test_media_kind_run_function(self) -> None:
        """fs.media handler run function is read_media."""
        from shell.fs.media import read_media

        handler = handler_for("fs.media")
        assert handler.run is read_media
