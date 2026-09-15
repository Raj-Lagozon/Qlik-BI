"""Fixed values shared across the backend (no config/env knobs live here)."""

from __future__ import annotations


class JobStatus:
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    ALL = (UPLOADED, QUEUED, RUNNING, SUCCESS, FAILED)
    TERMINAL = (SUCCESS, FAILED)


class PipelineStage:
    EXTRACT = "extract"
    CONVERT = "convert"
    BUILD = "build"


LOG_POLL_DEFAULT_SINCE = 0
