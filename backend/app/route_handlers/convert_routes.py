"""Per-stage + full conversion endpoints: convert/script (LOAD script ->
M query + variables), convert/data-model (relationships + RLS),
convert/sheet (master measures/dimensions/visuals/KPI containers), and
convert (all three, in order) — extracted/<app_name>/ -> converted/<app_name>/.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import JobStatus
from app.exception import JobStateError
from app.services.convert_services import (
    start_convert_all,
    start_convert_data_model,
    start_convert_script,
    start_convert_sheet,
)
from app.services.job_store import job_store

router = APIRouter(prefix="/api/jobs", tags=["conversion"])


def _guard_running(job_id: str):
    job = job_store.get(job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise JobStateError("Job is already running.")
    return job


@router.post("/{job_id}/convert/script")
async def convert_script_job(job_id: str):
    job = _guard_running(job_id)
    start_convert_script(job_id, job.app_name)
    return {"job_id": job_id, "status": JobStatus.QUEUED}


@router.post("/{job_id}/convert/data-model")
async def convert_data_model_job(job_id: str):
    job = _guard_running(job_id)
    start_convert_data_model(job_id, job.app_name)
    return {"job_id": job_id, "status": JobStatus.QUEUED}


@router.post("/{job_id}/convert/sheet")
async def convert_sheet_job(job_id: str):
    job = _guard_running(job_id)
    start_convert_sheet(job_id, job.app_name)
    return {"job_id": job_id, "status": JobStatus.QUEUED}


@router.post("/{job_id}/convert")
async def convert_all_job(job_id: str):
    job = _guard_running(job_id)
    start_convert_all(job_id, job.app_name)
    return {"job_id": job_id, "status": JobStatus.QUEUED}
