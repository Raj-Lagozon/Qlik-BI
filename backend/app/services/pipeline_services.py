"""Runs the FULL extract -> convert -> build pipeline in-process, in a
background thread, streaming its normal print() progress output into the
job's log. This is the ".qvf straight to PowerBI" endpoint kept exactly as
it always worked — one upload, one run, one download — for a user who
doesn't need the per-stage endpoints (extract / convert/script /
convert/data-model / convert/sheet / convert / build).

This calls the exact same functions the per-stage endpoints call —
`extract` (services/extract_services), `convert_all` (modules/convert_modules),
`build_project` (modules/build) — nothing about extraction/conversion/build
logic is reimplemented here. Any task that genuinely needs LLM judgment
continues to be driven entirely by `app/prompts/*.skill.md`; this module is
purely an orchestration + log relay for the web UI.
"""

from __future__ import annotations

import os
import threading

from app.config import JobStatus
from app.services.job_store import job_store
from app.logger import capture_stdout_lines

# The underlying pipeline modules use plain print() for progress, with no
# concept of "which job". Serializing runs with one lock lets us capture
# that output correctly (a single global stdout redirect) without having to
# thread every print() call through job-aware logging.
_PIPELINE_LOCK = threading.Lock()


def _run(job_id: str, app_name: str, qvf_path: str) -> None:
    with _PIPELINE_LOCK:
        job_store.set_status(job_id, JobStatus.RUNNING)
        on_line = lambda line: job_store.append_log(job_id, line)  # noqa: E731
        try:
            with capture_stdout_lines(on_line):
                # Imported here (not at module load) so the lock is held
                # for the whole run, including first-import side effects.
                from app.services.extract_services import extract
                from app.modules.convert_modules import convert_all
                from app.modules.build.project import build_project

                extract(app_name, qvf_path)
                convert_all(app_name)
                pbix_path = build_project(app_name)

            project_dir = os.path.dirname(pbix_path)
            job_store.set_result(job_id, pbix_path=pbix_path, project_dir=project_dir)
            job_store.set_status(job_id, JobStatus.SUCCESS)
        except Exception as exc:  # noqa: BLE001 - surfaced to the job log, not swallowed
            job_store.append_log(job_id, f"[backend] ERROR: {exc}")
            job_store.set_result(job_id, error=str(exc))
            job_store.set_status(job_id, JobStatus.FAILED)


def start_job(job_id: str, app_name: str, qvf_path: str) -> None:
    job_store.set_status(job_id, JobStatus.QUEUED)
    thread = threading.Thread(target=_run, args=(job_id, app_name, qvf_path), daemon=True)
    thread.start()
