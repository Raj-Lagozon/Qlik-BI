"""Upload endpoint, plus the standalone `/extract` job step (pull
script/data-model/measures/dimensions/sheets/variables/section-access out of
a .qvf into extracted/<app_name>/, nothing else)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, UploadFile

from app.config import JobStatus
from app.exception import JobStateError
from app.services.extract_step_services import start_extract
from app.services.job_store import job_store
from app.utilities.file_utils import sanitize_app_name, save_upload

router = APIRouter(prefix="/api/jobs", tags=["extraction"])


@router.post("/upload")
async def upload_qvf(file: UploadFile):
    app_name = sanitize_app_name(Path(file.filename or "app").stem)
    job = job_store.create(app_name=app_name, qvf_path="")
    saved_path = await save_upload(job.id, file)
    job.qvf_path = str(saved_path)
    return {"job_id": job.id, "app_name": job.app_name, "status": job.status}


@router.post("/{job_id}/extract")
async def extract_job(job_id: str):
    job = job_store.get(job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise JobStateError("Job is already running.")
    start_extract(job_id, job.app_name, job.qvf_path)
    return {"job_id": job_id, "status": JobStatus.QUEUED}
