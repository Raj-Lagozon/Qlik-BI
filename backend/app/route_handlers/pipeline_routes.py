"""Run / status / logs / download endpoints for the FULL .qvf -> PowerBI
pipeline (kept exactly as it always worked), plus the job status/logs/
download endpoints shared by every other stage endpoint (extract, convert,
build)."""

from __future__ import annotations

from pathlib import Path

from starlette.background import BackgroundTask

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.config import JobStatus, LOG_POLL_DEFAULT_SINCE
from app.exception import JobStateError
from app.services.pipeline_services import start_job
from app.services.job_store import job_store
from app.utilities.zip_utils import zip_pbip_project

router = APIRouter(prefix="/api/jobs", tags=["pipeline"])


@router.post("/{job_id}/run")
async def run_job(job_id: str):
    job = job_store.get(job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise JobStateError("Job is already running.")
    start_job(job_id, job.app_name, job.qvf_path)
    return {"job_id": job_id, "status": JobStatus.QUEUED}


@router.get("/{job_id}")
async def get_job(job_id: str):
    return job_store.get(job_id).snapshot()


@router.get("/{job_id}/logs")
async def get_logs(job_id: str, since: int = LOG_POLL_DEFAULT_SINCE):
    job = job_store.get(job_id)
    lines, next_offset = job_store.logs_since(job_id, since)
    return {
        "job_id": job_id,
        "status": job.status,
        "lines": lines,
        "next_offset": next_offset,
        "error": job.error,
    }


@router.get("/{job_id}/download")
@router.get("/{job_id}/download/pbix")
async def download_pbix(job_id: str):
    job = job_store.get(job_id)
    if job.status != JobStatus.SUCCESS or not job.pbix_path:
        raise JobStateError("Job has not produced a .pbix yet.")
    return FileResponse(
        job.pbix_path,
        media_type="application/octet-stream",
        filename=Path(job.pbix_path).name,
    )


@router.get("/{job_id}/download/pbip")
async def download_pbip(job_id: str):
    job = job_store.get(job_id)
    if job.status != JobStatus.SUCCESS or not job.project_dir:
        raise JobStateError("Job has not produced a .pbip project yet.")

    # The .pbip file is only a pointer — it needs its sibling .Report/ and
    # .SemanticModel/ folders next to it to open in Power BI Desktop, and
    # (unlike the .pbix) carries no row data at all, so it's zipped as one
    # downloadable bundle rather than served as a single file.
    zip_path = zip_pbip_project(Path(job.project_dir), job.app_name)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{job.app_name}.pbip.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )
