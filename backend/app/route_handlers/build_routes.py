"""Standalone `/build` job step: converted/<app_name>/ -> .pbip project +
compiled .pbix (same download endpoints in script_routes.py serve the result)."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import JobStatus
from app.exception import JobStateError
from app.services.build_services import start_build
from app.services.job_store import job_store

router = APIRouter(prefix="/api/jobs", tags=["build"])


@router.post("/{job_id}/build")
async def build_job(job_id: str):
    job = job_store.get(job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise JobStateError("Job is already running.")
    start_build(job_id, job.app_name)
    return {"job_id": job_id, "status": JobStatus.QUEUED}
