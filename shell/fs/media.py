"""Confined media observation: load an image from the workspace.

Vendors the minimal slice of colleague.media needed for the view_media handler:
_MEDIA_TYPES, validate_attachment, and build_part. The path must exist, be a
regular file within the configured root, be a known media type, and be at most
4 MiB. Images only — audio has no mid-work read use.

Does NOT vendor flatten_parts or IMAGE_TOKEN_ESTIMATE, which serve five
colleague-only call sites and remain in colleague/media.py.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from shell.environment import Environment
from shell.operations import ExecutionProfile, Operation, OperationIntent, register
from shell.results import OperationResult, OperationStatus

__all__ = ["read_media"]

#: Size cap for view_media tool — bounds wire + context cost; a typical
#: screenshot is well under it.
MAX_MEDIA_BYTES = 4 * 1024 * 1024

#: Extension → media type mapping. Subset includes both image and audio
#: for validation, but only images pass the handler.
_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "wav": "audio/wav",
    "mp3": "audio/mp3",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve *rel* under root, refusing any path that escapes it.

    Ported from colleague/tools.py:730.
    """
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path '{rel}' escapes the repo root")
    return candidate


def validate_attachment(path: str) -> dict[str, str]:
    """Validate path exists, is a regular file, has a known media extension.

    Returns a dict with keys ``path`` (str) and ``media_type`` (str).
    Raises ValueError for missing file, non-regular-file, unknown extension,
    or oversize file.

    Ported from colleague/media.py:36, adapted to not have the task-level
    size cap (MAX_ATTACHMENT_BYTES). The handler applies its own
    MAX_MEDIA_BYTES cap.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"Attachment file not found: {path}")
    if not p.is_file():
        raise ValueError(f"Attachment path is not a regular file: {path}")

    ext = p.suffix.lstrip(".").lower()
    if ext not in _MEDIA_TYPES:
        raise ValueError(f"Unknown attachment extension '{ext}' for {path}")

    return {"path": str(p), "media_type": _MEDIA_TYPES[ext]}


def build_part(attachment: dict[str, str]) -> dict[str, Any]:
    """Build a standard OpenAI content part from a validated attachment.

    * ``attachment`` is the dict returned by :func:`validate_attachment`.
    * Image attachments become ``{"type": "image_url", "image_url": ...}``.
    * Audio attachments become ``{"type": "input_audio", "input_audio": ...}``.

    Ported from colleague/media.py:66.
    """
    file_bytes = Path(attachment["path"]).read_bytes()
    encoded = base64.b64encode(file_bytes).decode("ascii")
    media_type = attachment["media_type"]

    if media_type.startswith("image/"):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
        }

    # audio
    ext = Path(attachment["path"]).suffix.lstrip(".").lower()
    return {
        "type": "input_audio",
        "input_audio": {"data": encoded, "format": ext},
    }


def read_media(operation: Operation, environment: Environment) -> OperationResult:
    """Load a workspace image as a base64-encoded content part.

    Pure read: same _safe_path confinement as file reading, a byte cap
    (MAX_MEDIA_BYTES) so one call cannot flood the wire/context, and
    images only — audio has no mid-work read use while the serving rig
    drops it, and validate_attachment already rejects non-media.

    Output is a provider-neutral media observation: a dict with:
    * result (str) - human readable summary
    * media_part (dict) - the structured base64-encoded image part

    Colleague adapts this neutral output into its own ToolOutcome shape.
    """
    try:
        path_arg = operation.arguments.get("path")
        if path_arg is None:
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error="fs.media requires 'path' argument",
                rendering="error: fs.media requires 'path' argument",
            )

        rel = str(path_arg)
        path = _safe_path(environment.work_root, rel)

        if not path.is_file():
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=f"no such file: {rel}",
                rendering=f"error: no such file: {rel}",
            )

        size = path.stat().st_size
        if size > MAX_MEDIA_BYTES:
            error = (
                f"cannot view {rel}: {size} bytes exceeds the "
                f"{MAX_MEDIA_BYTES}-byte media size cap"
            )
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=error,
                rendering=f"error: {error}",
            )

        try:
            attachment = validate_attachment(str(path))
        except ValueError as exc:
            error = str(exc)
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=error,
                rendering=f"error: {error}",
            )

        # Images only.
        if not attachment["media_type"].startswith("image/"):
            error = f"view_media is images only: {rel} is {attachment['media_type']}"
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=error,
                rendering=f"error: {error}",
            )

        try:
            part = build_part(attachment)
        except OSError as exc:
            error = f"cannot read {rel}: {exc}"
            return OperationResult(
                operation_id=operation.id,
                status=OperationStatus.FAILED,
                error=error,
                rendering=f"error: {error}",
            )

        result_text = f"loaded image {rel} ({size} bytes) into the conversation"
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.SUCCEEDED,
            output={
                "result": result_text,
                "media_part": part,
            },
            rendering=result_text,
        )

    except ValueError as exc:
        # Path escape or other validation error.
        error = str(exc)
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.FAILED,
            error=error,
            rendering=f"error: {error}",
        )
    except Exception as exc:
        # Any other exception becomes a failed result, never an uncaught crash.
        error = f"fs.media internal error: {exc}"
        return OperationResult(
            operation_id=operation.id,
            status=OperationStatus.FAILED,
            error=error,
            rendering=f"error: {error}",
        )


# Register the handler on module import using module-level registration.
# This ensures the handler is available when the module is imported.
_spec = register(
    "fs.media",
    intent=OperationIntent.OBSERVE,
    default_profile=ExecutionProfile.OBSERVE,
    run=read_media,
)
