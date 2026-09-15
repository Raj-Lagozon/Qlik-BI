"""Central exception types + FastAPI handler registration.

Every API-facing error should be raised as one of these so the response
shape is consistent (`{"error": "<code>", "detail": "..."}`) instead of
each route hand-rolling HTTPException calls.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 500
    error_code = "internal_error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class FileValidationError(AppError):
    status_code = 400
    error_code = "file_validation_error"


class JobNotFoundError(AppError):
    status_code = 404
    error_code = "job_not_found"


class JobStateError(AppError):
    """Raised when an action is requested against a job in the wrong state
    (e.g. re-running a job that's already running, downloading before it
    finished)."""

    status_code = 409
    error_code = "job_state_error"


class PipelineExecutionError(AppError):
    status_code = 500
    error_code = "pipeline_execution_error"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error_code, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )
