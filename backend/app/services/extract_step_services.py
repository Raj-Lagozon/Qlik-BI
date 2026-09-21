"""The standalone `/extract` job step (as opposed to `services.extract_services.extract`,
the plain function the full-pipeline `run` endpoint calls directly)."""

from __future__ import annotations

from app.services.extract_services import extract
from app.services.step_runner import start_step_background


def start_extract(job_id: str, app_name: str, qvf_path: str) -> None:
    start_step_background(job_id, "extract", lambda: extract(app_name, qvf_path))
