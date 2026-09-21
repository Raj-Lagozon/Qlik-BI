"""Generic single-step job runner shared by every stage endpoint (extract,
convert/script, convert/data-model, convert/sheet, convert, build) — each
just supplies the function to run; this handles the job lock, status
transitions, and streaming that function's print() output into the job's
log, the same way the full run/pipeline endpoint always has.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.config import JobStatus
from app.logger import capture_stdout_lines
from app.services.job_store import job_store

# Serializes runs so `capture_stdout_lines`'s single global stdout redirect
# is safe — only one job's output is ever being captured at a time.
_STEP_LOCK = threading.Lock()


def run_step_sync(job_id: str, step_name: str, fn: Callable[[], None]) -> None:
    """Runs `fn` synchronously (on the calling thread) with job-log capture.
    Used by the request handler itself for a single, normally-fast stage —
    the endpoint waits for it and returns the result directly."""
    with _STEP_LOCK:
        job_store.set_status(job_id, JobStatus.RUNNING)
        on_line = lambda line: job_store.append_log(job_id, line)  # noqa: E731
        try:
            with capture_stdout_lines(on_line):
                fn()
            job_store.set_status(job_id, JobStatus.SUCCESS)
        except Exception as exc:  # noqa: BLE001 - surfaced to the job log, not swallowed
            job_store.append_log(job_id, f"[backend] ERROR ({step_name}): {exc}")
            job_store.set_result(job_id, error=str(exc))
            job_store.set_status(job_id, JobStatus.FAILED)
            raise


def start_step_background(job_id: str, step_name: str, fn: Callable[[], None]) -> None:
    """Runs `fn` in a background thread with job-log capture — used for a
    stage that can take a while (extract, sheet conversion, full convert,
    build) so the endpoint can return immediately and the frontend polls
    /logs the same way it already does for the full run."""
    job_store.set_status(job_id, JobStatus.QUEUED)

    def _run() -> None:
        with _STEP_LOCK:
            job_store.set_status(job_id, JobStatus.RUNNING)
            on_line = lambda line: job_store.append_log(job_id, line)  # noqa: E731
            try:
                with capture_stdout_lines(on_line):
                    fn()
                job_store.set_status(job_id, JobStatus.SUCCESS)
            except Exception as exc:  # noqa: BLE001
                job_store.append_log(job_id, f"[backend] ERROR ({step_name}): {exc}")
                job_store.set_result(job_id, error=str(exc))
                job_store.set_status(job_id, JobStatus.FAILED)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
