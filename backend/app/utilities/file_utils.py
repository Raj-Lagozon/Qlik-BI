"""Filesystem helpers for handling uploaded .qvf files."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import UploadFile

from app.setting import settings
from app.exception import FileValidationError

_SAFE_NAME_RE = re.compile(r"[^\w\s\-–—]", re.UNICODE)


def sanitize_app_name(raw_name: str) -> str:
    """Keep the Qlik app's own name intact (letters/digits/spaces/dashes/em-dash)
    but strip anything that would be unsafe as a folder name — the pipeline
    treats app_name verbatim as a directory name under extracted/converted/output.
    """
    name = _SAFE_NAME_RE.sub("", raw_name).strip()
    if not name:
        raise FileValidationError("Could not derive a valid app name from the uploaded file name.")
    return name


def validate_upload(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise FileValidationError(
            f"Unsupported file type '{suffix or '(none)'}'. Only .qvf files are accepted."
        )
    return suffix


async def save_upload(job_id: str, file: UploadFile) -> Path:
    suffix = validate_upload(file)
    job_dir = settings.uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / f"source{suffix}"

    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise FileValidationError("Uploaded file exceeds the maximum allowed size.")
            out.write(chunk)

    if size == 0:
        dest.unlink(missing_ok=True)
        raise FileValidationError("Uploaded file is empty.")

    return dest
