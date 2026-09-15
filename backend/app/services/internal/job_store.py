"""In-memory job registry.

A "job" tracks one uploaded .qvf through extract -> convert -> build. State
lives in-process (a dict guarded by a lock) since this is a single-worker
dev/demo API, not a distributed service — restarting the API loses job
history, which is acceptable here since the durable artifacts (extracted/,
converted/, output/) survive on disk regardless.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from app.constant.constants import JobStatus
from app.exceptions import JobNotFoundError


@dataclass
class Job:
    id: str
    app_name: str
    qvf_path: str
    status: str = JobStatus.UPLOADED
    logs: list[str] = field(default_factory=list)
    pbix_path: str | None = None
    project_dir: str | None = None
    error: str | None = None

    def snapshot(self) -> dict:
        return {
            "job_id": self.id,
            "app_name": self.app_name,
            "status": self.status,
            "error": self.error,
            "has_pbix": self.pbix_path is not None,
            "has_pbip": self.project_dir is not None,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, app_name: str, qvf_path: str) -> Job:
        job = Job(id=str(uuid.uuid4()), app_name=app_name, qvf_path=qvf_path)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No job with id '{job_id}'.")
        return job

    def append_log(self, job_id: str, line: str) -> None:
        job = self.get(job_id)
        with self._lock:
            job.logs.append(line)

    def set_status(self, job_id: str, status: str) -> None:
        job = self.get(job_id)
        with self._lock:
            job.status = status

    def set_result(
        self,
        job_id: str,
        *,
        pbix_path: str | None = None,
        project_dir: str | None = None,
        error: str | None = None,
    ) -> None:
        job = self.get(job_id)
        with self._lock:
            job.pbix_path = pbix_path
            job.project_dir = project_dir
            job.error = error

    def logs_since(self, job_id: str, offset: int) -> tuple[list[str], int]:
        job = self.get(job_id)
        with self._lock:
            new_lines = job.logs[offset:]
            return new_lines, len(job.logs)


job_store = JobStore()
