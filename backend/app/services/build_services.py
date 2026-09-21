"""The standalone `/build` job step: assembles converted/<app>/ into a
.pbip project and compiles a .pbix, recording both paths on the job the
same way the full pipeline's `run` endpoint always has.
"""

from __future__ import annotations

import os

from app.modules.build.project import build_project
from app.services.job_store import job_store
from app.services.step_runner import start_step_background


def start_build(job_id: str, app_name: str) -> None:
    def _run() -> None:
        pbix_path = build_project(app_name)
        project_dir = os.path.dirname(pbix_path)
        job_store.set_result(job_id, pbix_path=pbix_path, project_dir=project_dir)

    start_step_background(job_id, "build", _run)
